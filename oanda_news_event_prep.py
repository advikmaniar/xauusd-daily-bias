"""
oanda_news_event_prep.py — pre-news trade prep for gold (XAU_USD).

Run it shortly before a scheduled high-impact US release (CPI, NFP, PCE,
FOMC, ...) to get everything needed to plan a trade on the print:

  - Live bid/ask/spread and time to the release.
  - D1 technicals: SMA20/50/200, EMA21, RSI14 (Wilder), ATR14, returns,
    where price sits in its 20-day range, trend label.
  - Liquidity: prior day / prior week / 20-day high-low, today's range so
    far, nearby round numbers.
  - HTF zones (unmitigated D1/H4 order blocks + FVGs) via oanda_htf_zones.
  - OANDA order book, collapsed into $10 clusters: significant buy walls
    below price (support) and sell walls above (resistance), plus where
    resting orders thin out (where a break can accelerate).
  - OANDA position book: retail long/short split, read as a contrarian
    signal.
  - How gold reacted to the same event on past release dates (first
    15 minutes, first 2 hours, by the NY close) and how often the first
    spike faded — split by whether the print was hot / in line / soft vs
    consensus and whether that surprise pushed gold with or against its
    trend at the time, with a confidence label on every rate. (A "faded
    3/3" pattern from mixed surprises failed on the 2026-10-14 CPI, when a
    hot core print in a downtrend just kept trending.)
  - Merged key levels (above / below) ranked by how many independent
    sources agree, an expected-move estimate, a technical/positioning bias
    score, and hot/soft scenario targets with invalidation.

What it does NOT do: fetch the macro side (consensus, prior print, Fed
pricing, headlines). That needs the web, so the routine running this
script adds it — see ROUTINE PROMPT below.

Usage:
  python oanda_news_event_prep.py --event CPI --date 2026-10-14
  python oanda_news_event_prep.py --event NFP --date 2026-11-06 \\
      --past 2026-10-02:hot,2026-09-04:soft,2026-08-07:inline
  python oanda_news_event_prep.py --event CPI --date 2026-10-14 --json

  --et-time       release time in New York time (default 08:30). Converted
                  per date, so past reactions stay right across DST.
  --past          comma-separated past release dates, each optionally
                  tagged with how the print compared with consensus:
                  YYYY-MM-DD[:hot|inline|soft]. If omitted, uses
                  KNOWN_EVENT_DATES for the event. The --date itself is
                  always skipped, so the history never includes the print
                  being prepared for.
  --hot-direction which way gold is expected to move on a hotter-than-
                  expected (hawkish) print. "down" for CPI/NFP/PCE (default).

ROUTINE PROMPT (paste into the "Gold News Prep" routine):
  1. Run: python oanda_news_event_prep.py --event <EVENT> --date <DATE> --json
     (look up the past release dates of the event, and whether each
     print came in hot / inline / soft vs consensus, and pass them with
     --past if they aren't in KNOWN_EVENT_DATES).
  2. Web-search the release: consensus and prior for headline and core
     (MoM + YoY), what drives the forecast, current Fed pricing (CME
     FedWatch), dollar/yields/oil, and the latest gold headlines.
  3. Write a short trade-prep brief: bias, key levels, order-book walls,
     sentiment, what hot / in-line / soft prints mean for gold with
     targets and invalidation, and execution notes (spread, fade vs
     chase). Only lean on a fade/hold rate when its confidence isn't
     "low"; otherwise say the history is too thin. Cite sources.
  4. After the release, add the date and its hot/inline/soft label to
     KNOWN_EVENT_DATES so the history grows.

Env vars required:
  OANDA_API_TOKEN
  OANDA_ACCOUNT_ID
"""

import argparse
import json
import os
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import oanda_htf_zones as z

BASE_URL = z.BASE_URL
INSTRUMENT = z.INSTRUMENT
NY = ZoneInfo("America/New_York")

# Past releases per event, used when --past isn't given: date -> how the
# print compared with consensus ("hot" = hawkish surprise, "inline",
# "soft", or None if unknown). For CPI the label follows core MoM. Extend
# this after each print (the time comes from --et-time).
KNOWN_EVENT_DATES = {
    "CPI": {
        "2026-10-14": "hot",     # core +0.3% vs +0.2%
        "2026-09-11": "hot",     # core +0.3% vs +0.2%
        "2026-08-12": "inline",  # core +0.2% vs +0.2%
        "2026-07-14": "soft",    # core  0.0% vs +0.2%
    },
}
SURPRISES = ("hot", "inline", "soft")

# Below this many samples a fade/hold rate is anecdote, not evidence.
CONFIDENCE_BANDS = ((10, "reasonable"), (5, "moderate"), (0, "low"))

CLUSTER_BIN = 10.0          # $ width of an order-book cluster
LEVEL_MERGE_ATR = 0.15      # levels closer than this (in D1 ATR) merge into one
ROUND_STEP = 50.0           # round-number spacing


# --------------------------------------------------------------- fetch --

def get_pricing():
    url = f"{BASE_URL}/v3/accounts/{os.environ['OANDA_ACCOUNT_ID']}/pricing"
    r = requests.get(url, headers=z._headers(), params={"instruments": INSTRUMENT}, timeout=10)
    r.raise_for_status()
    p = r.json()["prices"][0]
    bid, ask = float(p["bids"][0]["price"]), float(p["asks"][0]["price"])
    return {"bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 2),
            "spread": round(ask - bid, 2), "time": p["time"]}


def get_book(kind):
    """kind: 'orderBook' or 'positionBook'. Returns (snapshot_time, price, buckets)
    where buckets are [(price, long_pct, short_pct)]."""
    url = f"{BASE_URL}/v3/instruments/{INSTRUMENT}/{kind}"
    r = requests.get(url, headers=z._headers(), timeout=15)
    r.raise_for_status()
    book = r.json()[kind]
    buckets = [(float(b["price"]), float(b["longCountPercent"]), float(b["shortCountPercent"]))
               for b in book["buckets"]]
    return book["time"], float(book["price"]), buckets


# ---------------------------------------------------------- indicators --

def sma(closes, n):
    return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None


def ema(closes, n):
    if len(closes) < n:
        return None
    k = 2 / (n + 1)
    e = sum(closes[:n]) / n
    for c in closes[n:]:
        e = c * k + e * (1 - k)
    return round(e, 2)


def rsi(closes, period=14):
    """Wilder's RSI, same formula as oanda_xauusd_report_v2.rsi."""
    if len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0) for c in changes]
    losses = [max(-c, 0) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 2)


def pct_return(closes, n):
    return round((closes[-1] / closes[-1 - n] - 1) * 100, 2) if len(closes) > n else None


def technicals(d1, price):
    closes = [c["close"] for c in d1]
    last20 = d1[-20:]
    hi20, lo20 = max(c["high"] for c in last20), min(c["low"] for c in last20)
    t = {
        "sma20": sma(closes, 20), "sma50": sma(closes, 50), "sma200": sma(closes, 200),
        "ema21": ema(closes, 21), "rsi14": rsi(closes), "atr14": z.atr(d1, 14),
        "ret_5d_pct": pct_return(closes, 5), "ret_20d_pct": pct_return(closes, 20),
        "ret_60d_pct": pct_return(closes, 60),
        "range_20d": {"high": hi20, "low": lo20,
                      "position_pct": round((price - lo20) / (hi20 - lo20) * 100, 1) if hi20 > lo20 else None},
    }
    above = sum(1 for k in ("sma20", "sma50", "sma200") if t[k] and price > t[k])
    t["trend"] = {3: "bullish", 2: "leaning bullish", 1: "leaning bearish", 0: "bearish"}[above]
    return t


# ------------------------------------------------------- order book ----

def cluster_book(buckets, price, span, bin_size=CLUSTER_BIN):
    """Collapse $0.50 buckets into `bin_size` bins within +-span of price.
    Returns bins sorted by price: {low, high, buy, sell, total, side}."""
    bins = {}
    for p, buy, sell in buckets:
        if abs(p - price) > span:
            continue
        k = (p // bin_size) * bin_size
        b = bins.setdefault(k, [0.0, 0.0])
        b[0] += buy
        b[1] += sell
    out = []
    for k in sorted(bins):
        buy, sell = bins[k]
        side = "below" if k + bin_size <= price else "above" if k > price else "at"
        out.append({"low": k, "high": k + bin_size, "buy": round(buy, 2), "sell": round(sell, 2),
                    "total": round(buy + sell, 2), "side": side})
    return out


def significant_walls(bins, top=3, min_ratio=1.5):
    """Support = buy-dominant bins below price, resistance = sell-dominant
    bins above, each kept only if its dominant side is >= min_ratio x the
    median bin total. Strongest first."""
    if not bins:
        return {"support": [], "resistance": []}
    med = statistics.median(b["total"] for b in bins) or 0.01
    support = [b for b in bins if b["side"] in ("below", "at") and b["buy"] > b["sell"] and b["buy"] >= min_ratio * med]
    resistance = [b for b in bins if b["side"] in ("above", "at") and b["sell"] > b["buy"] and b["sell"] >= min_ratio * med]
    support.sort(key=lambda b: -b["buy"])
    resistance.sort(key=lambda b: -b["sell"])
    return {"support": support[:top], "resistance": resistance[:top]}


def thin_zone(bins, price, direction, min_ratio=0.5):
    """First price, moving away from `price` in `direction` ('down'/'up'),
    past which the next two bins both hold < min_ratio x median orders —
    i.e. where resting orders run out and a break can accelerate."""
    if not bins:
        return None
    med = statistics.median(b["total"] for b in bins) or 0.01
    side = [b for b in bins if (b["high"] <= price if direction == "down" else b["low"] >= price)]
    side.sort(key=lambda b: -b["low"] if direction == "down" else b["low"])
    thin = [b["total"] < min_ratio * med for b in side]
    for i in range(len(side)):
        # a thin bin at the edge of the scanned span counts as running out
        if thin[i] and (i + 1 == len(side) or thin[i + 1]):
            return side[i]["high"] if direction == "down" else side[i]["low"]
    return None


def book_imbalance(bins, price, span):
    """Buy orders within `span` below price vs sell orders within `span` above."""
    buys_below = sum(b["buy"] for b in bins if b["high"] <= price and price - b["high"] <= span)
    sells_above = sum(b["sell"] for b in bins if b["low"] >= price and b["low"] - price <= span)
    return {"buys_below": round(buys_below, 2), "sells_above": round(sells_above, 2)}


def positioning(buckets):
    long_pct = sum(b[1] for b in buckets)
    short_pct = sum(b[2] for b in buckets)
    total = long_pct + short_pct or 1
    long_share = round(long_pct / total * 100, 1)
    if long_share >= 65:
        read = "crowded long (contrarian bearish)"
    elif long_share <= 35:
        read = "crowded short (contrarian bullish)"
    else:
        read = "balanced"
    return {"long_pct": long_share, "short_pct": round(100 - long_share, 1), "read": read}


# --------------------------------------------------- past reactions ----

def release_utc(date_str, et_time):
    hh, mm = map(int, et_time.split(":"))
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY).astimezone(timezone.utc)


def reaction_stats(m5, release):
    """m5: M5 candles covering the release day. Moves are $ relative to
    the last close before the release. Returns None if data is missing or
    the day hasn't reached the NY close yet (a partial day isn't scored)."""
    def t(c):
        return datetime.fromisoformat(c["time"][:19]).replace(tzinfo=timezone.utc)

    before = [c for c in m5 if t(c) < release]
    if not before:
        return None
    pre = before[-1]["close"]
    ny_close = release.astimezone(NY).replace(hour=16, minute=0).astimezone(timezone.utc)

    def window(minutes=None, end=None):
        end = end or release + timedelta(minutes=minutes)
        s = [c for c in m5 if release <= t(c) < end]
        if not s:
            return None
        return {"low": round(min(c["low"] for c in s) - pre, 1),
                "high": round(max(c["high"] for c in s) - pre, 1),
                "close": round(s[-1]["close"] - pre, 1)}

    w15, w2h, wday = window(15), window(120), window(end=ny_close)
    if not (w15 and w2h and wday) or t(m5[-1]) < ny_close - timedelta(minutes=5):
        return None
    # first spike = the larger excursion in the first 15 min
    spike = w15["high"] if abs(w15["high"]) >= abs(w15["low"]) else w15["low"]
    peak = wday["high"] if abs(wday["high"]) >= abs(wday["low"]) else wday["low"]
    faded = abs(wday["close"]) <= abs(spike) / 2 or (spike * wday["close"] < 0)
    return {"pre": pre, "first_15m": w15, "first_2h": w2h, "to_ny_close": wday,
            "first_spike": spike, "peak_move": peak, "spike_faded_by_half": faded}


def trend_at(d1_before):
    """Gold's D1 trend going into a release: last close vs SMA20, using
    only candles that closed before it."""
    closes = [c["close"] for c in d1_before]
    s20 = sma(closes, 20)
    if s20 is None:
        return None
    return "bullish" if closes[-1] > s20 else "bearish"


def surprise_gold_direction(surprise, hot_direction):
    """Which way a print's surprise pushes gold: hot -> hot_direction,
    soft -> the opposite, inline -> no push."""
    if surprise == "hot":
        return hot_direction
    if surprise == "soft":
        return "up" if hot_direction == "down" else "down"
    return None


def alignment(surprise, trend, hot_direction):
    """'with_trend' if the surprise pushes gold the way it was already
    trending, 'against_trend' if the other way, None if no push / unknown."""
    push = surprise_gold_direction(surprise, hot_direction)
    if push is None or trend is None:
        return None
    return "with_trend" if (push == "down") == (trend == "bearish") else "against_trend"


def confidence(n):
    for floor, label in CONFIDENCE_BANDS:
        if n >= floor:
            return label


def fade_rate(rs):
    n = len(rs)
    faded = sum(1 for r in rs if r["spike_faded_by_half"])
    return {"faded": faded, "samples": n, "confidence": confidence(n)}


def summarize_reactions(reactions):
    rs = [r for r in reactions.values() if r]
    if not rs:
        return None
    by_surprise = {k: fade_rate([r for r in rs if r.get("surprise") == k]) for k in SURPRISES}
    by_alignment = {k: fade_rate([r for r in rs if r.get("alignment") == k])
                    for k in ("with_trend", "against_trend")}
    return {
        "samples": len(rs),
        "avg_abs_first_spike": round(statistics.mean(abs(r["first_spike"]) for r in rs), 1),
        "avg_abs_peak_move": round(statistics.mean(abs(r["peak_move"]) for r in rs), 1),
        "faded_count": sum(1 for r in rs if r["spike_faded_by_half"]),
        "confidence": confidence(len(rs)),
        "by_surprise": {k: v for k, v in by_surprise.items() if v["samples"]},
        "by_alignment": {k: v for k, v in by_alignment.items() if v["samples"]},
    }


def fade_guide(summary, current_trend, hot_direction):
    """For each possible print, whether it would push gold with or against
    today's trend, and the matching historical fade rate."""
    guide = {}
    for surprise in ("hot", "soft"):
        al = alignment(surprise, current_trend, hot_direction)
        hist = (summary or {}).get("by_alignment", {}).get(al) or {"faded": 0, "samples": 0, "confidence": "low"}
        if hist["confidence"] == "low":
            read = "history too thin — don't assume a fade"
        elif hist["faded"] * 2 > hist["samples"]:
            read = "first spike has tended to fade"
        else:
            read = "first spike has tended to hold — don't fade it"
        guide[surprise] = {"gold": surprise_gold_direction(surprise, hot_direction),
                           "alignment": al, "history": hist, "read": read}
    return guide


# ------------------------------------------------------- key levels ----

def merge_levels(candidates, atr_value, price):
    """candidates: [(price, tag)]. Groups levels lying within LEVEL_MERGE_ATR*ATR
    of a group's first (lowest) level, so a run of nearby levels can't chain
    into one wide blob. Each group becomes one averaged level with every tag."""
    tol = (atr_value or 10) * LEVEL_MERGE_ATR
    merged = []
    for p, tag in sorted(candidates):
        if merged and p - merged[-1]["prices"][0] <= tol:
            merged[-1]["prices"].append(p)
            merged[-1]["tags"].append(tag)
        else:
            merged.append({"prices": [p], "tags": [tag]})
    out = []
    for m in merged:
        lvl = round(sum(m["prices"]) / len(m["prices"]), 2)
        out.append({"price": lvl, "tags": sorted(set(m["tags"])), "strength": len(set(m["tags"])),
                    "distance": round(lvl - price, 2)})
    above = sorted([l for l in out if l["price"] > price], key=lambda l: l["price"])
    below = sorted([l for l in out if l["price"] <= price], key=lambda l: -l["price"])
    return above, below


def level_candidates(price, tech, liq, walls, zones, thin_down, thin_up, atr_value, span):
    c = []
    for k in ("sma20", "sma50", "sma200", "ema21"):
        if tech.get(k):
            c.append((tech[k], k.upper()))
    c += [(tech["range_20d"]["high"], "20d high"), (tech["range_20d"]["low"], "20d low")]
    for name, lvl in liq.items():
        c += [(lvl["high"], f"{name} high"), (lvl["low"], f"{name} low")]
    for w in walls["support"]:
        c.append(((w["low"] + w["high"]) / 2, f"buy wall {w['buy']}%"))
    for w in walls["resistance"]:
        c.append(((w["low"] + w["high"]) / 2, f"sell wall {w['sell']}%"))
    for tf in ("d1", "h4"):
        for ob in zones[tf]["order_blocks"]:
            edge = ob["high"] if ob["low"] < price else ob["low"]
            c.append((edge, f"{tf.upper()} {ob['type']} OB"))
        for f in zones[tf]["fvgs"]:
            edge = f["top"] if f["bottom"] < price else f["bottom"]
            c.append((edge, f"{tf.upper()} {f['type']} FVG"))
    if thin_down:
        c.append((thin_down, "orders thin below"))
    if thin_up:
        c.append((thin_up, "orders thin above"))
    r = (price // ROUND_STEP) * ROUND_STEP
    for k in range(-3, 4):
        c.append((r + k * ROUND_STEP, "round number"))
    return [(p, tag) for p, tag in c if abs(p - price) <= span]


# ------------------------------------------------------------- bias ----

def bias_score(price, tech, pos, imbalance):
    """Simple additive score, positive = bullish. Each input is +-1."""
    parts = {}
    for k in ("sma20", "sma50", "sma200"):
        if tech.get(k):
            parts[f"price vs {k}"] = 1 if price > tech[k] else -1
    r = tech.get("rsi14")
    if r is not None:
        parts["rsi14"] = 1 if r > 55 else -1 if r < 45 else 0
    parts["retail positioning (contrarian)"] = (-1 if pos["long_pct"] >= 65 else 1 if pos["long_pct"] <= 35 else 0)
    b, s = imbalance["buys_below"], imbalance["sells_above"]
    parts["order book (buys below vs sells above)"] = 1 if b > 1.5 * s else -1 if s > 1.5 * b else 0
    score = sum(parts.values())
    label = ("bullish" if score >= 3 else "leaning bullish" if score >= 1 else
             "bearish" if score <= -3 else "leaning bearish" if score <= -1 else "neutral")
    return {"score": score, "max": len(parts), "label": label, "components": parts}


def scenarios(price, above, below, expected_move, hot_direction):
    """Targets = strongest levels within 1.5x expected move in each direction."""
    reach = expected_move * 1.5

    def targets(levels):
        near = [l for l in levels if abs(l["distance"]) <= reach]
        strong = [l for l in near if l["strength"] >= 2] or near
        return [l["price"] for l in strong[:3]]

    up, down = targets(above), targets(below)
    hot, soft = (down, up) if hot_direction == "down" else (up, down)
    return {
        "expected_move": round(expected_move, 1),
        "hot_print": {"gold": hot_direction, "targets": hot},
        "soft_print": {"gold": "up" if hot_direction == "down" else "down", "targets": soft},
        "in_line": {"range": [down[0] if down else None, up[0] if up else None]},
    }


# -------------------------------------------------------------- main ----

def parse_past(spec):
    """'2026-09-11:hot,2026-08-12' -> {'2026-09-11': 'hot', '2026-08-12': None}"""
    out = {}
    for item in filter(None, (x.strip() for x in spec.split(","))):
        date, _, surprise = item.partition(":")
        if surprise and surprise not in SURPRISES:
            raise ValueError(f"unknown surprise label {surprise!r} for {date} (use {'/'.join(SURPRISES)})")
        out[date] = surprise or None
    return out


def build_report(event, date_str, et_time, past, hot_direction):
    """past: {date: surprise label or None}."""
    live = get_pricing()
    price = live["mid"]
    release = release_utc(date_str, et_time)

    d1 = z.get_candles("D", count=260)
    h4 = z.get_candles("H4", count=300)
    w = z.get_candles("W", count=52)
    m = z.get_candles("M", count=24)
    today = z.get_candles("M15", count=96)

    tech = technicals(d1, price)
    atr_value = tech["atr14"]
    span = 2.5 * atr_value

    zones = z.compute_htf_features(d1, h4, w, m, price)
    liq = dict(zones["liquidity"])
    day_start = datetime.now(timezone.utc).astimezone(NY).replace(hour=17, minute=0, second=0, microsecond=0)
    if day_start > datetime.now(timezone.utc):
        day_start -= timedelta(days=1)  # gold's trading day opens 17:00 New York
    session = [c for c in today if datetime.fromisoformat(c["time"][:19]).replace(tzinfo=timezone.utc) >= day_start]
    # skip today's range until it's wide enough to mean anything
    if session and max(c["high"] for c in session) - min(c["low"] for c in session) >= 0.3 * atr_value:
        liq["today_so_far"] = {"high": max(c["high"] for c in session), "low": min(c["low"] for c in session)}

    ob_time, _, ob_buckets = get_book("orderBook")
    pb_time, _, pb_buckets = get_book("positionBook")
    bins = cluster_book(ob_buckets, price, span)
    walls = significant_walls(bins)
    thin_down, thin_up = thin_zone(bins, price, "down"), thin_zone(bins, price, "up")
    imbalance = book_imbalance(bins, price, atr_value)
    pos = positioning(pb_buckets)

    def iso(t):
        return t.isoformat().replace("+00:00", "Z")

    reactions, pending = {}, []
    for d, surprise in sorted(past.items(), reverse=True):
        if d == date_str:
            continue  # never count the print we're preparing for
        rel = release_utc(d, et_time)
        if rel.astimezone(NY).replace(hour=16, minute=0) > datetime.now(timezone.utc):
            pending.append(d)  # NY close not reached yet, nothing to score
            continue
        try:
            to = min(rel + timedelta(hours=9), datetime.now(timezone.utc) - timedelta(minutes=1))
            m5 = z.get_candles("M5", frm=iso(rel - timedelta(hours=1)), to=iso(to))
            stats = reaction_stats(m5, rel)
            if stats:
                d1_before = z.get_candles("D", frm=iso(rel - timedelta(days=45)), to=iso(rel - timedelta(hours=12)))
                trend = trend_at(d1_before)
                stats.update(surprise=surprise, trend=trend, alignment=alignment(surprise, trend, hot_direction))
            reactions[d] = stats
        except requests.HTTPError:
            reactions[d] = None
    summary = summarize_reactions(reactions)
    current_trend = "bullish" if tech["sma20"] and price > tech["sma20"] else "bearish"

    above, below = merge_levels(
        level_candidates(price, tech, liq, walls, zones, thin_down, thin_up, atr_value, span), atr_value, price)
    expected_move = max(summary["avg_abs_peak_move"] if summary else 0, 0.75 * atr_value)

    return {
        "event": event,
        "release_utc": release.isoformat(),
        "release_ny": release.astimezone(NY).strftime("%a %d %b %H:%M ET"),
        "hours_to_release": round((release - datetime.now(timezone.utc)).total_seconds() / 3600, 1),
        "live": live,
        "technicals": tech,
        "liquidity": liq,
        "order_book": {"snapshot": ob_time, "walls": walls, "thin_below": thin_down,
                       "thin_above": thin_up, "imbalance_within_1atr": imbalance},
        "positioning": {"snapshot": pb_time, **pos},
        "past_reactions": reactions,
        "past_reaction_summary": summary,
        "past_reactions_pending": pending,
        "fade_guide": fade_guide(summary, current_trend, hot_direction),
        "key_levels": {"above": above[:6], "below": below[:6]},
        "bias": bias_score(price, tech, pos, imbalance),
        "scenarios": scenarios(price, above, below, expected_move, hot_direction),
    }


def print_report(r):
    t, live = r["technicals"], r["live"]
    print(f"{r['event']} — {r['release_ny']} ({r['release_utc'][11:16]} UTC), in {r['hours_to_release']}h")
    print(f"XAU_USD {live['bid']} / {live['ask']}  spread {live['spread']}  @ {live['time'][:19]}Z\n")

    print(f"BIAS  {r['bias']['label'].upper()}  ({r['bias']['score']:+d} of ±{r['bias']['max']})")
    for k, v in r["bias"]["components"].items():
        print(f"  {v:+d}  {k}")

    print(f"\nTREND {t['trend']}  |  RSI {t['rsi14']}  ATR {t['atr14']}  |  "
          f"SMA20 {t['sma20']}  SMA50 {t['sma50']}  SMA200 {t['sma200']}")
    print(f"      5d {t['ret_5d_pct']}%  20d {t['ret_20d_pct']}%  |  20d range "
          f"{t['range_20d']['low']}–{t['range_20d']['high']} (at {t['range_20d']['position_pct']}%)")

    p = r["positioning"]
    print(f"\nRETAIL  {p['long_pct']}% long / {p['short_pct']}% short — {p['read']}")
    ob = r["order_book"]
    imb = ob["imbalance_within_1atr"]
    print(f"BOOK    buys within 1 ATR below {imb['buys_below']}%  vs  sells within 1 ATR above {imb['sells_above']}%")
    for w in ob["walls"]["resistance"]:
        print(f"  sell wall {w['low']:.0f}–{w['high']:.0f}  {w['sell']}%")
    for w in ob["walls"]["support"]:
        print(f"  buy wall  {w['low']:.0f}–{w['high']:.0f}  {w['buy']}%")
    print(f"  orders thin below {ob['thin_below']}  /  above {ob['thin_above']}")

    print("\nKEY LEVELS (strength = independent sources agreeing)")
    for l in reversed(r["key_levels"]["above"]):
        print(f"  R {l['price']:>9.2f}  {'*' * l['strength']:<5} {', '.join(l['tags'])}")
    print(f"  ── {live['mid']:.2f} ──")
    for l in r["key_levels"]["below"]:
        print(f"  S {l['price']:>9.2f}  {'*' * l['strength']:<5} {', '.join(l['tags'])}")

    s = r["past_reaction_summary"]
    print("\nPAST REACTIONS ($ from pre-release price)")
    for d, x in r["past_reactions"].items():
        if not x:
            print(f"  {d}  no data")
            continue
        tag = f"{x.get('surprise') or '?':6s} {(x.get('alignment') or '-').replace('_', ' '):13s}"
        print(f"  {d}  {tag}  first spike {x['first_spike']:+.1f}  peak {x['peak_move']:+.1f}  "
              f"NY close {x['to_ny_close']['close']:+.1f}  {'faded' if x['spike_faded_by_half'] else 'held'}")
    for d in r["past_reactions_pending"]:
        print(f"  {d}  not scored yet (before NY close)")
    if s:
        print(f"  avg spike ±{s['avg_abs_first_spike']}  avg peak ±{s['avg_abs_peak_move']}  "
              f"faded {s['faded_count']}/{s['samples']} overall ({s['confidence']} confidence)")
        for k, v in {**s["by_surprise"], **s["by_alignment"]}.items():
            print(f"    {k.replace('_', ' '):13s} faded {v['faded']}/{v['samples']}  ({v['confidence']} confidence)")
    print("  FADE OR CHASE?")
    for k, g in r["fade_guide"].items():
        h = g["history"]
        print(f"    {k:4s} print -> gold {g['gold']:4s} ({(g['alignment'] or '?').replace('_', ' ')}): "
              f"faded {h['faded']}/{h['samples']} — {g['read']}")

    sc = r["scenarios"]
    print(f"\nSCENARIOS (expected move ±{sc['expected_move']})")
    print(f"  hot print  -> gold {sc['hot_print']['gold']:4s} targets {sc['hot_print']['targets']}")
    print(f"  soft print -> gold {sc['soft_print']['gold']:4s} targets {sc['soft_print']['targets']}")
    print(f"  in line    -> chop {sc['in_line']['range'][0]}–{sc['in_line']['range'][1]}")
    print("\nMacro side (consensus, prior, Fed pricing, headlines) is added by the routine — see module docstring.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--event", default="CPI")
    ap.add_argument("--date", required=True, help="release date, YYYY-MM-DD")
    ap.add_argument("--et-time", default="08:30", help="release time in New York (HH:MM)")
    ap.add_argument("--past", help="comma-separated past releases, YYYY-MM-DD[:hot|inline|soft]")
    ap.add_argument("--hot-direction", choices=("up", "down"), default="down")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    past = parse_past(a.past) if a.past else dict(KNOWN_EVENT_DATES.get(a.event.upper(), {}))
    report = build_report(a.event.upper(), a.date, a.et_time, past, a.hot_direction)
    if a.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
