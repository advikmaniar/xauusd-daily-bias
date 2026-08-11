"""
oanda_xauusd_report.py  (multi-timeframe version)

Pulls live XAUUSD price + multi-timeframe technical stats for the daily bias
routine. Outputs a single JSON object to stdout — nothing else — so Claude
can parse it directly in the routine session.

Timeframes analyzed: D1 (macro trend/SMA), H4 (swing structure), H1 (session
ranges + intraday trend), M15 (near-term momentum). Using several timeframes
means the bias isn't based on one snapshot — it checks whether higher and
lower timeframes actually agree before calling a direction.

Env vars required (set in the Claude Code routine's Environment, not here):
  OANDA_API_TOKEN
  OANDA_ACCOUNT_ID
"""

import os
import json
import requests
from datetime import datetime, timezone

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


def swing_high_low(candles, lookback=20):
    """Most recent swing high/low over the lookback window — proxy for
    structure levels (BOS/CHoCH reference points) on that timeframe."""
    window = candles[-lookback:]
    if not window:
        return None
    return {
        "high": round(max(c["high"] for c in window), 2),
        "low": round(min(c["low"] for c in window), 2),
        "lookback_candles": len(window),
    }


def timeframe_trend(candles, sma_period=20):
    """Simple per-timeframe trend read: last close vs its own SMA, plus
    whether the last 3 candles are making higher highs/lows or lower
    highs/lows (basic structure momentum check)."""
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
    }


def session_range(candles, start_h, end_h):
    today = datetime.now(timezone.utc).date()
    rows = [c for c in candles
            if datetime.fromisoformat(c["time"].replace("Z", "+00:00")).date() == today
            and start_h <= datetime.fromisoformat(c["time"].replace("Z", "+00:00")).hour < end_h]
    if not rows:
        return None
    return {"high": round(max(c["high"] for c in rows), 2), "low": round(min(c["low"] for c in rows), 2)}


def multi_tf_alignment(tf_trends):
    """Counts how many analyzed timeframes agree on direction. This is the
    key 'strengthen the analysis' signal — a bias backed by 4/4 timeframes
    agreeing is materially stronger than one based on D1 alone."""
    biases = [t["bias_vs_sma20"] for t in tf_trends.values() if t]
    bullish = biases.count("bullish")
    bearish = biases.count("bearish")
    total = len(biases)
    if bullish == total and total > 0:
        verdict = "fully_aligned_bullish"
    elif bearish == total and total > 0:
        verdict = "fully_aligned_bearish"
    elif bullish > bearish:
        verdict = "mostly_bullish"
    elif bearish > bullish:
        verdict = "mostly_bearish"
    else:
        verdict = "mixed_no_agreement"
    return {"verdict": verdict, "bullish_count": bullish, "bearish_count": bearish, "total_timeframes": total}


def main():
    live = get_live_price()

    d1 = get_candles("D", 110)
    h4 = get_candles("H4", 120)
    h1 = get_candles("H1", 100)
    m15 = get_candles("M15", 100)

    tf_trends = {
        "D1": timeframe_trend(d1),
        "H4": timeframe_trend(h4),
        "H1": timeframe_trend(h1),
        "M15": timeframe_trend(m15),
    }

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "day_of_week": datetime.now(timezone.utc).strftime("%A"),
        "live_price": live,
        "prior_day": {
            "high": d1[-2]["high"], "low": d1[-2]["low"], "close": d1[-2]["close"],
        } if len(d1) >= 2 else None,
        "sma_50_d1": sma(d1, 50),
        "sma_100_d1": sma(d1, 100),
        "atr_14_d1": atr(d1, 14),
        "asian_range_utc_00_08": session_range(h1, 0, 8),
        "london_range_utc_08_13": session_range(h1, 8, 13),
        "timeframe_trends": tf_trends,
        "swing_structure": {
            "D1": swing_high_low(d1, 20),
            "H4": swing_high_low(h4, 20),
            "H1": swing_high_low(h1, 20),
            "M15": swing_high_low(m15, 20),
        },
        "multi_timeframe_alignment": multi_tf_alignment(tf_trends),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
