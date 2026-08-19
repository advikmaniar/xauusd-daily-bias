"""
oanda_xauusd_report_v2.py  (intraday-weighted + liquidity/fib confluence version)

Used by the "Gold Bias V2" routine only — V1's routine keeps using the
original oanda_xauusd_report.py. Keep both files in sync manually if you
change shared logic; they're intentionally separate so V1 stays untouched.

Pulls live XAUUSD price + multi-timeframe technical stats, split into two
groups:
  STRUCTURAL  = D1, H4  -> the "big picture" trend, used as context and for
                           setting invalidation levels, NOT the primary
                           driver of today's actionable bias.
  INTRADAY    = H1, M15, M5 -> what price is actually doing right now.
                           This group drives the actual BUY/SELL/NONE call
                           for the trading day.

Also explicitly detects reversal/pullback conditions: if price has pulled
back meaningfully from a recent swing extreme and lower timeframes show a
consistent counter-structural move (e.g. lower-highs/lower-lows while D1/H4
are still bullish), that gets flagged as reversal_against_structure so the
routine can catch exactly the situation described: HTF bullish, but today
is actually a down day.

On top of that, computes liquidity levels (prior day/session/week high-low)
and fibonacci retracement levels off the most recent H1 swing, then flags
confluence_zones where a liquidity level and a fib level land close
together — used to judge whether a pullback is exhausting near a level
rather than continuing through it.

Env vars required (set in the Claude Code routine's Environment):
  OANDA_API_TOKEN
  OANDA_ACCOUNT_ID
"""

import os
import json
import requests
from datetime import datetime, timedelta, timezone

OANDA_API_TOKEN = os.environ["OANDA_API_TOKEN"]
OANDA_ACCOUNT_ID = os.environ["OANDA_ACCOUNT_ID"]
BASE_URL = "https://api-fxpractice.oanda.com"
INSTRUMENT = "XAU_USD"
HEADERS = {"Authorization": f"Bearer {OANDA_API_TOKEN}"}


def get_live_price():
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
    r = requests.get(url, headers=HEADERS, params={"instruments": INSTRUMENT}, timeout=10)
    r.raise_for_status()
    p = r.json()["prices"][0]
    bid, ask = float(p["bids"][0]["price"]), float(p["asks"][0]["price"])
    return {"bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 2), "time": p["time"]}


def get_candles(granularity, count):
    url = f"{BASE_URL}/v3/instruments/{INSTRUMENT}/candles"
    r = requests.get(url, headers=HEADERS,
                      params={"granularity": granularity, "count": count, "price": "M"},
                      timeout=10)
    r.raise_for_status()
    out = []
    for c in r.json()["candles"]:
        if not c["complete"]:
            continue
        out.append({
            "time": c["time"],
            "open": float(c["mid"]["o"]), "high": float(c["mid"]["h"]),
            "low": float(c["mid"]["l"]), "close": float(c["mid"]["c"]),
        })
    return out


def sma(candles, period):
    closes = [c["close"] for c in candles[-period:]]
    return round(sum(closes) / len(closes), 2) if len(closes) == period else None


def atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    trs = trs[-period:]
    return round(sum(trs) / len(trs), 2) if len(trs) == period else None


def rsi(candles, period=14):
    """Wilder's RSI — the standard formula most platforms (including
    TradingView's default) use. Average gain/loss is recursively smoothed
    over the whole available history, not a flat average of the last
    `period` changes, so it carries a fading memory of moves older than
    `period` candles instead of dropping them off a cliff."""
    if len(candles) < period + 1:
        return None
    changes = [candles[i]["close"] - candles[i - 1]["close"] for i in range(1, len(candles))]
    gains = [max(c, 0) for c in changes]
    losses = [max(-c, 0) for c in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def consecutive_streak(candles, max_n=6):
    """How many consecutive candles have closed in the same direction, most
    recent first. Catches a live pullback (e.g. 4 straight down closes)
    even if the broader timeframe SMA hasn't flipped yet."""
    closes = [c["close"] for c in candles[-(max_n + 1):]]
    diffs = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    if not diffs:
        return {"direction": None, "count": 0}
    direction = "up" if diffs[-1] > 0 else "down" if diffs[-1] < 0 else None
    if direction is None:
        return {"direction": None, "count": 0}
    streak = 0
    for d in reversed(diffs):
        cur = "up" if d > 0 else "down" if d < 0 else None
        if cur == direction:
            streak += 1
        else:
            break
    return {"direction": direction, "count": streak}


def timeframe_trend(candles, sma_period=20):
    if len(candles) < sma_period + 3:
        return None
    tf_sma = sma(candles, sma_period)
    last_close = candles[-1]["close"]
    bias = "bullish" if last_close > tf_sma else "bearish" if last_close < tf_sma else "flat"

    last3 = candles[-3:]
    higher_highs = last3[2]["high"] > last3[1]["high"] > last3[0]["high"]
    lower_lows = last3[2]["low"] < last3[1]["low"] < last3[0]["low"]
    structure = "hh_hl" if higher_highs else "lh_ll" if lower_lows else "mixed"

    return {
        "sma20": tf_sma,
        "last_close": last_close,
        "bias_vs_sma20": bias,
        "recent_structure": structure,
        "rsi_14": rsi(candles, 14),
        "streak": consecutive_streak(candles),
    }


def swing_high_low(candles, lookback=20):
    window = candles[-lookback:]
    if not window:
        return None
    return {
        "high": round(max(c["high"] for c in window), 2),
        "low": round(min(c["low"] for c in window), 2),
        "lookback_candles": len(window),
    }


def recent_extreme(h1_candles, lookback_hours=72):
    """Finds the most significant recent high/low (default: last 72h on H1)
    and how far/how long since price has moved away from it. This is what
    catches 'peaked 2 days ago, been fading since' — the exact scenario
    from the chart: a $4,435 high days back with a persistent slide after."""
    window = h1_candles[-lookback_hours:]
    if not window:
        return None
    high_idx = max(range(len(window)), key=lambda i: window[i]["high"])
    low_idx = min(range(len(window)), key=lambda i: window[i]["low"])
    current = h1_candles[-1]["close"]
    high_val, low_val = window[high_idx]["high"], window[low_idx]["low"]
    return {
        "recent_high": round(high_val, 2),
        "hours_since_high": len(window) - 1 - high_idx,
        "pct_from_high": round((current - high_val) / high_val * 100, 2),
        "recent_low": round(low_val, 2),
        "hours_since_low": len(window) - 1 - low_idx,
        "pct_from_low": round((current - low_val) / low_val * 100, 2),
    }


def session_range(candles, start_h, end_h):
    today = datetime.now(timezone.utc).date()
    rows = [c for c in candles
            if datetime.fromisoformat(c["time"].replace("Z", "+00:00")).date() == today
            and start_h <= datetime.fromisoformat(c["time"].replace("Z", "+00:00")).hour < end_h]
    if not rows:
        return None
    return {"high": round(max(c["high"] for c in rows), 2), "low": round(min(c["low"] for c in rows), 2)}


SESSIONS = [("Asian", 0, 9), ("London", 7, 16), ("NY", 12, 21)]


def session_range_for_date(candles, date, start_h, end_h):
    rows = [c for c in candles
            if datetime.fromisoformat(c["time"].replace("Z", "+00:00")).date() == date
            and start_h <= datetime.fromisoformat(c["time"].replace("Z", "+00:00")).hour < end_h]
    if not rows:
        return None
    return {"high": round(max(c["high"] for c in rows), 2), "low": round(min(c["low"] for c in rows), 2)}


def previous_session(h1_candles):
    """The most recently *completed* session (Asian/London/NY), which may be
    from earlier today or from yesterday if today's first session hasn't
    closed yet. Used as a liquidity reference distinct from the full prior
    day's high/low."""
    now = datetime.now(timezone.utc)
    candidates = []
    for day_offset in (1, 0):
        d = (now - timedelta(days=day_offset)).date()
        for name, start_h, end_h in SESSIONS:
            end_dt = datetime(d.year, d.month, d.day, end_h % 24, tzinfo=timezone.utc)
            if end_h == 24:
                end_dt += timedelta(days=1)
            if end_dt <= now:
                candidates.append((end_dt, name, d, start_h, end_h))
    if not candidates:
        return None
    end_dt, name, d, start_h, end_h = max(candidates, key=lambda c: c[0])
    rng = session_range_for_date(h1_candles, d, start_h, end_h)
    if not rng:
        return None
    return {"name": name, "date": d.isoformat(), **rng}


def previous_week_range(d1_candles):
    """High/low of the most recent fully completed ISO week, from daily
    candles — a longer-horizon liquidity reference than prior day/session."""
    current_week = datetime.now(timezone.utc).isocalendar()[:2]
    weeks = {}
    for c in d1_candles:
        dt = datetime.fromisoformat(c["time"].replace("Z", "+00:00"))
        wk = dt.isocalendar()[:2]
        if wk == current_week:
            continue
        weeks.setdefault(wk, []).append(c)
    if not weeks:
        return None
    last_week = max(weeks.keys())
    rows = weeks[last_week]
    dates = sorted(datetime.fromisoformat(c["time"].replace("Z", "+00:00")).date() for c in rows)
    return {
        "week_start": dates[0].isoformat(),
        "week_end": dates[-1].isoformat(),
        "high": round(max(c["high"] for c in rows), 2),
        "low": round(min(c["low"] for c in rows), 2),
    }


def fib_retracement(extreme):
    """Standard fib retracement levels across the most recent significant
    swing (the 72h H1 extreme). Direction follows whichever leg is more
    recent: high after low = up-swing (retracement support below the high),
    low after high = down-swing (retracement resistance above the low)."""
    if not extreme:
        return None
    high, low = extreme["recent_high"], extreme["recent_low"]
    span = high - low
    if span <= 0:
        return None
    up_swing = extreme["hours_since_high"] <= extreme["hours_since_low"]
    ratios = [0.0, 23.6, 38.2, 50.0, 61.8, 78.6, 100.0]
    levels = {}
    for r in ratios:
        frac = r / 100
        levels[str(r)] = round(high - span * frac, 2) if up_swing else round(low + span * frac, 2)
    return {
        "swing_direction": "up" if up_swing else "down",
        "swing_low": low,
        "swing_high": high,
        "levels": levels,
    }


def annotate_distances(levels_flat, current_price, near_threshold_pct=0.15):
    """levels_flat: dict of {label: price}. Adds pct distance from current
    price and a `near` flag so the routine doesn't have to do this math
    itself (and can't silently skip it)."""
    out = {}
    for label, price in levels_flat.items():
        if price is None:
            continue
        pct = round(abs(current_price - price) / current_price * 100, 3)
        out[label] = {"price": price, "distance_pct": pct, "near": pct <= near_threshold_pct}
    return out


def find_confluence(liquidity_flat, fib_flat, current_price, cluster_tolerance_pct=0.25, relevance_pct=2.0):
    """Flags pairs of liquidity levels and fib levels that sit close to each
    other (not just close to price) — a real confluence zone, which is what
    actually matters for reversal odds. Only surfaces zones within
    `relevance_pct` of current price since this report is for today's bias."""
    zones = []
    for liq_name, liq_price in liquidity_flat.items():
        if liq_price is None:
            continue
        for fib_name, fib_price in fib_flat.items():
            if fib_price is None:
                continue
            cluster_pct = abs(liq_price - fib_price) / current_price * 100
            if cluster_pct > cluster_tolerance_pct:
                continue
            avg_price = round((liq_price + fib_price) / 2, 2)
            price_distance_pct = round(abs(current_price - avg_price) / current_price * 100, 3)
            if price_distance_pct > relevance_pct:
                continue
            zones.append({
                "liquidity_level": liq_name,
                "liquidity_price": liq_price,
                "fib_level": fib_name,
                "fib_price": fib_price,
                "zone_price": avg_price,
                "distance_from_price_pct": price_distance_pct,
                "price_inside_zone": price_distance_pct <= 0.15,
            })
    zones.sort(key=lambda z: z["distance_from_price_pct"])
    return zones


def group_alignment(trends_dict):
    biases = [t["bias_vs_sma20"] for t in trends_dict.values() if t]
    bullish, bearish, total = biases.count("bullish"), biases.count("bearish"), len(biases)
    if total == 0:
        verdict = "no_data"
    elif bullish == total:
        verdict = "fully_aligned_bullish"
    elif bearish == total:
        verdict = "fully_aligned_bearish"
    elif bullish > bearish:
        verdict = "mostly_bullish"
    elif bearish > bullish:
        verdict = "mostly_bearish"
    else:
        verdict = "mixed_no_agreement"
    return {"verdict": verdict, "bullish_count": bullish, "bearish_count": bearish, "total_timeframes": total}


def detect_reversal(structural_alignment, intraday_alignment, extreme):
    """The key new signal: flags when intraday price action is moving
    opposite to the structural (D1/H4) trend, which is exactly the case
    that produced a wrong BUY call while price slid all day."""
    struct_dir = "bullish" if "bullish" in structural_alignment["verdict"] else \
                 "bearish" if "bearish" in structural_alignment["verdict"] else None
    intra_dir = "bullish" if "bullish" in intraday_alignment["verdict"] else \
                "bearish" if "bearish" in intraday_alignment["verdict"] else None

    if not struct_dir or not intra_dir or struct_dir == intra_dir:
        return {"flag": False, "detail": "Structural and intraday trend are not in conflict."}

    pct_move = extreme["pct_from_high"] if intra_dir == "bearish" else extreme["pct_from_low"]
    return {
        "flag": True,
        "detail": (
            f"Structural trend is {struct_dir} (D1/H4) but intraday timeframes "
            f"(H1/M15/M5) are {intra_dir}. Price is {pct_move}% from its recent "
            f"extreme. This is a pullback/reversal against the higher-timeframe "
            f"trend — today's actionable bias should follow the INTRADAY "
            f"direction, not the structural one."
        ),
    }


def main():
    live = get_live_price()

    d1 = get_candles("D", 110)
    h4 = get_candles("H4", 120)
    h1 = get_candles("H1", 100)
    m15 = get_candles("M15", 100)
    m5 = get_candles("M5", 150)

    structural_trends = {"D1": timeframe_trend(d1), "H4": timeframe_trend(h4)}
    intraday_trends = {"H1": timeframe_trend(h1), "M15": timeframe_trend(m15), "M5": timeframe_trend(m5)}

    structural_alignment = group_alignment(structural_trends)
    intraday_alignment = group_alignment(intraday_trends)
    extreme = recent_extreme(h1, 72)
    reversal = detect_reversal(structural_alignment, intraday_alignment, extreme)

    prior_day = {"high": d1[-1]["high"], "low": d1[-1]["low"], "close": d1[-1]["close"]} if len(d1) >= 1 else None
    prior_sess = previous_session(h1)
    prior_week = previous_week_range(d1)
    fib = fib_retracement(extreme)

    liquidity_flat = {}
    if prior_day:
        liquidity_flat["prior_day_high"] = prior_day["high"]
        liquidity_flat["prior_day_low"] = prior_day["low"]
    if prior_sess:
        liquidity_flat[f"prior_session_{prior_sess['name'].lower()}_high"] = prior_sess["high"]
        liquidity_flat[f"prior_session_{prior_sess['name'].lower()}_low"] = prior_sess["low"]
    if prior_week:
        liquidity_flat["prior_week_high"] = prior_week["high"]
        liquidity_flat["prior_week_low"] = prior_week["low"]

    fib_flat = {("fib_" + k): v for k, v in fib["levels"].items()} if fib else {}

    key_levels = annotate_distances({**liquidity_flat, **fib_flat}, live["mid"])
    confluence_zones = find_confluence(liquidity_flat, fib_flat, live["mid"])

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "day_of_week": datetime.now(timezone.utc).strftime("%A"),
        "live_price": live,
        "prior_day": prior_day,
        "sma_50_d1": sma(d1, 50),
        "sma_100_d1": sma(d1, 100),
        "atr_14_d1": atr(d1, 14),
        "asian_range_utc_00_08": session_range(h1, 0, 8),
        "london_range_utc_08_13": session_range(h1, 8, 13),

        "structural_trends": structural_trends,
        "intraday_trends": intraday_trends,
        "structural_alignment": structural_alignment,
        "intraday_alignment": intraday_alignment,
        "recent_extreme_72h": extreme,
        "reversal_against_structure": reversal,

        "swing_structure": {
            "D1": swing_high_low(d1, 20),
            "H4": swing_high_low(h4, 20),
            "H1": swing_high_low(h1, 20),
            "M15": swing_high_low(m15, 20),
        },

        "prior_session": prior_sess,
        "prior_week": prior_week,
        "fibonacci": fib,
        "key_level_distances": key_levels,
        "confluence_zones": confluence_zones,
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()