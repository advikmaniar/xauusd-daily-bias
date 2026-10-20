"""
Unit tests for oanda_scalp_snapshot.py, against synthetic data (no
network calls). Run directly:

  python test_oanda_scalp_snapshot.py
"""

import os
os.environ.setdefault("OANDA_API_TOKEN", "unused-in-tests")
os.environ.setdefault("OANDA_ACCOUNT_ID", "unused-in-tests")

from datetime import datetime, timezone

import requests

import oanda_scalp_snapshot as s

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def test_floor_pivots():
    print("\n-- floor_pivots --")
    p = s.floor_pivots({"high": 110, "low": 90, "close": 100})
    check("P/R1/S1/R2/S2", p == {"P": 100, "R1": 110, "R2": 120, "S1": 90, "S2": 80}, p)


def test_book_bins():
    print("\n-- book_bins --")
    buckets = [(100.0, 0.3, 0.0), (102.5, 0.3, 0.0),   # 100-105 bin: buy 0.6
               (110.0, 0.1, 0.1),                      # below threshold
               (200.0, 5.0, 5.0)]                      # outside span
    b = s.book_bins(buckets, 105.0)
    check("one significant bin", len(b) == 1 and b[0]["low"] == 100 and b[0]["buy_pct"] == 0.6, b)


def test_today_range():
    print("\n-- today_range --")
    now = datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc)
    c = [{"time": "2026-10-19T23:00:00Z", "high": 999, "low": 1},
         {"time": "2026-10-20T01:00:00Z", "high": 105, "low": 95},
         {"time": "2026-10-20T02:00:00Z", "high": 108, "low": 97}]
    check("only today's bars", s.today_range(c, now) == {"high": 108, "low": 95})
    check("no bars -> None", s.today_range(c[:1], now) is None)


def test_with_retry():
    print("\n-- with_retry --")
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise requests.exceptions.HTTPError("401")
        return "ok"

    check("retries until success", s.with_retry(flaky, wait=0) == "ok" and len(calls) == 3)
    try:
        s.with_retry(lambda: (_ for _ in ()).throw(requests.exceptions.HTTPError("x")), tries=2, wait=0)
        check("gives up after tries", False)
    except requests.exceptions.HTTPError:
        check("gives up after tries", True)


if __name__ == "__main__":
    test_floor_pivots()
    test_book_bins()
    test_today_range()
    test_with_retry()
    print(f"\n{'ALL PASSED' if not FAILURES else f'{len(FAILURES)} FAILED: {FAILURES}'}")
    raise SystemExit(1 if FAILURES else 0)
