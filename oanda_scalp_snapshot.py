"""
oanda_scalp_snapshot.py — intraday scalping snapshot for gold (XAU_USD).

Run by the "XAUUSD Scalp" routine, which the app's Scalping screen fires
on demand. Prints the raw material for one scalp read as JSON:

  - Live bid/ask/mid/spread.
  - H1 / M15 / M5 ATR14, M15 EMA20/50 + RSI14, M5 EMA20 + RSI14.
  - Prior-day floor pivots (P, R1/R2, S1/S2) and today's UTC high/low.
  - The last 8 H1 and 8 M15 candles (what just happened).
  - OANDA order book in $5 bins within ±$40 of price, only bins with real
    size, so the read can name where resting buy/sell orders cluster.
  - Retail long % from the position book (contrarian read).

The routine turns this (plus headlines) into the "XAUUSD Scalp" calendar
event the app's Scalping screen shows — format in
xauusd-bot-app/scalp_report.py.

OANDA's practice market-data endpoints throw intermittent 401/5xx even
with a valid token, so every fetch is retried.

  python oanda_scalp_snapshot.py
"""

import json
import time
from datetime import datetime, timezone

import requests

import oanda_htf_zones as z
import oanda_news_event_prep as n

BOOK_SPAN = 40.0      # $ either side of price to read the order book
BOOK_BIN = 5.0        # $ width of an order-book bin
BOOK_MIN_PCT = 0.45   # bins with less buy+sell % than this are noise


def with_retry(fn, *args, tries=15, wait=1.5, **kwargs):
    for attempt in range(tries):
        try:
            return fn(*args, **kwargs)
        except requests.exceptions.RequestException:
            if attempt == tries - 1:
                raise
            time.sleep(wait)


def floor_pivots(day):
    h, l, c = day["high"], day["low"], day["close"]
    p = (h + l + c) / 3
    return {k: round(v, 2) for k, v in {
        "P": p, "R1": 2 * p - l, "R2": p + (h - l), "S1": 2 * p - h, "S2": p - (h - l),
    }.items()}


def book_bins(buckets, price, span=BOOK_SPAN, bin_size=BOOK_BIN, min_pct=BOOK_MIN_PCT):
    """Collapse order-book buckets into $bin_size bins near price, high to low."""
    bins = {}
    for q, buy, sell in buckets:
        if abs(q - price) > span:
            continue
        lo = q // bin_size * bin_size
        b = bins.setdefault(lo, [0.0, 0.0])
        b[0] += buy
        b[1] += sell
    return [{"low": lo, "high": lo + bin_size, "buy_pct": round(b, 2), "sell_pct": round(s, 2)}
            for lo, (b, s) in sorted(bins.items(), reverse=True) if b + s >= min_pct]


def today_range(candles, now):
    today = now.strftime("%Y-%m-%d")
    bars = [c for c in candles if c["time"].startswith(today)]
    if not bars:
        return None
    return {"high": max(c["high"] for c in bars), "low": min(c["low"] for c in bars)}


def bars(candles, k, time_slice):
    return [{"time": c["time"][time_slice], "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"]}
            for c in candles[-k:]]


def build_snapshot():
    now = datetime.now(timezone.utc)
    price = with_retry(n.get_pricing)
    d1 = with_retry(z.get_candles, "D", count=5)
    h1 = with_retry(z.get_candles, "H1", count=30)
    m15 = with_retry(z.get_candles, "M15", count=80)
    m5 = with_retry(z.get_candles, "M5", count=40)
    book_time, _, order_book = with_retry(n.get_book, "orderBook")
    _, _, position_book = with_retry(n.get_book, "positionBook")

    c15 = [c["close"] for c in m15]
    c5 = [c["close"] for c in m5]
    return {
        "time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": price,
        "atr": {"H1": z.atr(h1), "M15": z.atr(m15), "M5": z.atr(m5)},
        "m15": {"ema20": n.ema(c15, 20), "ema50": n.ema(c15, 50), "rsi14": n.rsi(c15)},
        "m5": {"ema20": n.ema(c5, 20), "rsi14": n.rsi(c5)},
        "prior_day": {"opened": d1[-1]["time"][:16], "high": d1[-1]["high"], "low": d1[-1]["low"],
                      "close": d1[-1]["close"], "pivots": floor_pivots(d1[-1])},
        "today": today_range(h1 + m15[-4:], now),
        "h1_last8": bars(h1, 8, slice(5, 16)),
        "m15_last8": bars(m15, 8, slice(11, 16)),
        "order_book": {"time": book_time, "bins": book_bins(order_book, price["mid"])},
        "retail_long_pct": n.positioning(position_book)["long_pct"],
    }


if __name__ == "__main__":
    print(json.dumps(build_snapshot(), indent=2))
