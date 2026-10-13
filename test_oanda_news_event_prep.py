"""
Unit tests for oanda_news_event_prep.py, against synthetic data (no
network calls). Run directly:

  python test_oanda_news_event_prep.py
"""

import os
os.environ.setdefault("OANDA_API_TOKEN", "unused-in-tests")
os.environ.setdefault("OANDA_ACCOUNT_ID", "unused-in-tests")

from datetime import datetime, timedelta, timezone

import oanda_news_event_prep as n

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def test_release_utc():
    print("\n-- release_utc --")
    check("08:30 ET in October (EDT) -> 12:30 UTC",
          n.release_utc("2026-10-14", "08:30").strftime("%H:%M") == "12:30")
    check("08:30 ET in December (EST) -> 13:30 UTC",
          n.release_utc("2026-12-10", "08:30").strftime("%H:%M") == "13:30")


def test_cluster_and_walls():
    print("\n-- cluster_book / significant_walls / thin_zone --")
    price = 100.0
    buckets = []
    for p in range(50, 151):  # $1 buckets, background 0.1% each side
        buy, sell = 0.1, 0.1
        if 80 <= p < 90:
            buy = 1.0   # buy wall below
        if 120 <= p < 130:
            sell = 1.0  # sell wall above
        if p < 60 or p >= 140:
            buy = sell = 0.0  # empty book far out
        buckets.append((float(p), buy, sell))
    bins = n.cluster_book(buckets, price, span=50, bin_size=10)
    walls = n.significant_walls(bins)
    check("buy wall found at 80-90", walls["support"] and walls["support"][0]["low"] == 80, walls)
    check("sell wall found at 120-130", walls["resistance"] and walls["resistance"][0]["low"] == 120, walls)
    check("orders thin below at 60", n.thin_zone(bins, price, "down") == 60, n.thin_zone(bins, price, "down"))
    check("orders thin above at 140", n.thin_zone(bins, price, "up") == 140, n.thin_zone(bins, price, "up"))


def test_positioning():
    print("\n-- positioning --")
    check("72/28 -> crowded long", "crowded long" in n.positioning([(1, 72, 28)])["read"])
    check("50/50 -> balanced", n.positioning([(1, 50, 50)])["read"] == "balanced")


def test_merge_levels_no_chaining():
    print("\n-- merge_levels --")
    # tol = 0.15 * 20 = 3. A run 100,102,104,106 must not collapse into one level.
    above, below = n.merge_levels([(100, "a"), (102, "b"), (104, "c"), (106, "d")], 20, 50)
    check("chain splits into 2 levels", len(above) == 2, above)
    check("first level merges a+b", above[0]["tags"] == ["a", "b"] and above[0]["price"] == 101.0, above[0])


def test_reaction_stats():
    print("\n-- reaction_stats --")
    rel = datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc)

    def c(minutes, o, h, l, cl):
        t = (rel + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
        return {"time": t, "open": o, "high": h, "low": l, "close": cl}

    m5 = [c(-5, 100, 100, 100, 100),          # pre-release close = 100
          c(0, 100, 101, 60, 70),              # spike down 40
          c(5, 70, 90, 70, 90),
          c(60, 90, 130, 90, 125),             # peak +30 later
          c(200, 125, 125, 105, 110)]          # NY close +10
    r = n.reaction_stats(m5, rel)
    check("first spike -40", r["first_spike"] == -40.0, r)
    check("peak picks larger excursion (-40 vs +30)", r["peak_move"] == -40.0, r)
    check("spike faded (closed on other side)", r["spike_faded_by_half"] is True, r)


def test_bias_score():
    print("\n-- bias_score --")
    tech = {"sma20": 110, "sma50": 120, "sma200": 130, "rsi14": 40}
    b = n.bias_score(100, tech, {"long_pct": 72}, {"buys_below": 1, "sells_above": 1})
    check("all bearish inputs -> bearish", b["label"] == "bearish" and b["score"] == -5, b)


if __name__ == "__main__":
    test_release_utc()
    test_cluster_and_walls()
    test_positioning()
    test_merge_levels_no_chaining()
    test_reaction_stats()
    test_bias_score()
    print(f"\n{'ALL PASSED' if not FAILURES else f'{len(FAILURES)} FAILED: {FAILURES}'}")
    raise SystemExit(1 if FAILURES else 0)
