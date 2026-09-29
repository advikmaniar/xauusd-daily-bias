"""
oanda_htf_zones.py — HTF (higher-timeframe) zone engine for gold.

Pulls D1, H4, Weekly and Monthly candles from OANDA and identifies the
classic ICT-style zones that inform a higher-timeframe bias:

  - Order blocks (bullish/bearish) — the last opposite-colored candle
    before a break-of-structure impulse, on D1 and H4.
  - Fair value gaps (bullish/bearish) — 3-candle imbalances, on D1 and H4.
  - Liquidity zones — prior day/week/month high-low, plus equal-highs /
    equal-lows clusters (swing points sitting within a tight tolerance of
    each other — classic resting-liquidity pools).
  - Fibonacci golden zone (61.8%-78.6%) off the most significant recent
    swing on D1 and H4.
  - Premium/discount position within the recent D1/H4 range.
  - A zone "gate" per direction (buy/sell): whether an unmitigated
    opposing zone sits within veto range, and how much room (in ATR)
    exists before the nearest one.

Two ways to use this file:

  1. As a script — prints a human-readable snapshot for the current
     live price:  python oanda_htf_zones.py

  2. As a library — `compute_htf_features(d1, h4, w, m, current_price)`
     returns a plain-dict snapshot with no I/O, so a backtester can call
     it once per as-of timestamp by slicing candle history up to that
     point and passing it in. Nothing in this module fetches data itself
     except `get_candles`/`get_live_price`, which the caller is free to
     skip when backtesting against pre-fetched history.

This is a standalone research module — NOT wired into app.py or either
routine script.

Env vars required (only for the script/live-fetch path):
  OANDA_API_TOKEN
  OANDA_ACCOUNT_ID

Definitions worth being explicit about (these are judgment calls, not
settled facts — expect the backtest to revise some of them):

  - An order block is "touched" once price trades back into its range at
    all, and "invalidated" only once price *closes* beyond its far edge.
    Zones are considered live ("unmitigated") until invalidated, not
    merely touched — a retest that holds is exactly the kind of reaction
    a filter should care about, not evidence the zone is dead.
  - An FVG is "partially filled" once price trades into the near edge of
    the gap, and "fully filled" once price trades through to the far
    edge. The gate logic below treats "fully filled" as dead.
  - relevance_score (0-1ish, uncapped) blends zone size vs ATR, the
    strength of the impulse that created it, and recency. It is a first
    pass, not a finished formula — expect the backtest to reweight or
    drop terms.
"""

import os
import requests
from datetime import datetime, timezone

BASE_URL = "https://api-fxpractice.oanda.com"
INSTRUMENT = "XAU_USD"


def _headers():
    return {"Authorization": f"Bearer {os.environ['OANDA_API_TOKEN']}"}


def get_live_price():
    url = f"{BASE_URL}/v3/accounts/{os.environ['OANDA_ACCOUNT_ID']}/pricing"
    r = requests.get(url, headers=_headers(), params={"instruments": INSTRUMENT}, timeout=10)
    r.raise_for_status()
    p = r.json()["prices"][0]
    bid, ask = float(p["bids"][0]["price"]), float(p["asks"][0]["price"])
    return round((bid + ask) / 2, 2)


def get_candles(granularity, count=None, frm=None, to=None):
    """count XOR (frm[, to]) — OANDA doesn't accept count together with
    from/to. frm/to let a backtest pull a specific historical window."""
    url = f"{BASE_URL}/v3/instruments/{INSTRUMENT}/candles"
    params = {"granularity": granularity, "price": "M"}
    if frm:
        params["from"] = frm
        if to:
            params["to"] = to
    else:
        params["count"] = count or 250
    r = requests.get(url, headers=_headers(), params=params, timeout=30)
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


# ----------------------------------------------------------------- ATR --

def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    trs = trs[-period:]
    return round(sum(trs) / len(trs), 2) if len(trs) == period else None


# ---------------------------------------------------------------- swings --

def detect_swings(candles, strength=2):
    """Fractal swing points: a candle whose high/low is the most extreme
    among `strength` candles on each side. Returns a list of
    {index, time, price, kind: 'high'|'low'}, oldest first."""
    swings = []
    for i in range(strength, len(candles) - strength):
        window = candles[i - strength:i + strength + 1]
        if candles[i]["high"] == max(c["high"] for c in window):
            swings.append({"index": i, "time": candles[i]["time"], "price": candles[i]["high"], "kind": "high"})
        if candles[i]["low"] == min(c["low"] for c in window):
            swings.append({"index": i, "time": candles[i]["time"], "price": candles[i]["low"], "kind": "low"})
    return swings


def detect_equal_levels(candles, swings, tolerance_pct=0.15):
    """Clusters of 2+ swing highs (or lows) sitting within tolerance_pct of
    each other — a classic resting-liquidity pool. Excludes clusters price
    has already traded through since the most recent touch — that
    liquidity has already been run and the level no longer functions as a
    pending target."""
    clusters = []
    for kind in ("high", "low"):
        points = sorted([s for s in swings if s["kind"] == kind], key=lambda s: s["price"])
        used = [False] * len(points)
        for i, p in enumerate(points):
            if used[i]:
                continue
            group = [p]
            used[i] = True
            for j in range(i + 1, len(points)):
                if used[j]:
                    continue
                if abs(points[j]["price"] - p["price"]) / p["price"] * 100 <= tolerance_pct:
                    group.append(points[j])
                    used[j] = True
            if len(group) < 2:
                continue

            last_touch_index = max(g["index"] for g in group)
            level = sum(g["price"] for g in group) / len(group)
            after = candles[last_touch_index + 1:]
            if kind == "high":
                swept = any(c["high"] > level for c in after)
            else:
                swept = any(c["low"] < level for c in after)
            if swept:
                continue

            clusters.append({
                "kind": f"equal_{kind}s",
                "price_avg": round(level, 2),
                "touches": len(group),
                "times": [g["time"] for g in group],
            })
    return clusters


# -------------------------------------------------------- relevance util --

def _relevance_score(size_atr_val, displacement_atr_val, age_candles, history_len):
    """0-1ish heuristic: bigger zone (vs ATR) + stronger impulse that
    created it + more recent = more relevant. Deliberately simple —
    revisit once the backtest has an opinion."""
    size_term = min(size_atr_val or 0, 2) / 2
    disp_term = min(displacement_atr_val or 0, 3) / 3
    age_frac = min(age_candles / history_len, 1) if history_len else 1
    recency_term = 1 - age_frac
    return round(0.4 * size_term + 0.4 * disp_term + 0.2 * recency_term, 3)


# ---------------------------------------------------------- order blocks --

def detect_order_blocks(candles, swings, atr_value=None, lookback_for_ob=15, max_results=5):
    """Last opposite-colored candle before a break-of-structure impulse.
    Bullish OB: last down-close candle before price closes back above a
    prior swing high. Bearish OB: last up-close candle before price
    closes back below a prior swing low.

    Returns both `touched` (price traded back into the range at all) and
    `invalidated` (price closed beyond the far edge — the zone is dead).
    Only non-invalidated ones are returned, sorted by relevance_score."""
    swing_highs = [s for s in swings if s["kind"] == "high"]
    swing_lows = [s for s in swings if s["kind"] == "low"]

    obs = []

    for sh in swing_highs:
        for j in range(sh["index"] + 1, len(candles)):
            if candles[j]["close"] > sh["price"]:
                for k in range(j - 1, max(j - lookback_for_ob, sh["index"]) - 1, -1):
                    if candles[k]["close"] < candles[k]["open"]:
                        after = candles[j + 1:]
                        impulse_high = max((c["high"] for c in candles[j:j + 3]), default=candles[j]["high"])
                        ob = {
                            "type": "bullish", "time": candles[k]["time"],
                            "high": candles[k]["high"], "low": candles[k]["low"],
                            "broke_structure_at": candles[j]["time"],
                            "touched": any(c["low"] <= candles[k]["high"] for c in after),
                            "invalidated": any(c["close"] < candles[k]["low"] for c in after),
                            "_end_index": k,
                            "_size_atr": (candles[k]["high"] - candles[k]["low"]) / atr_value if atr_value else None,
                            "_disp_atr": (impulse_high - candles[k]["close"]) / atr_value if atr_value else None,
                        }
                        obs.append(ob)
                        break
                break

    for sl in swing_lows:
        for j in range(sl["index"] + 1, len(candles)):
            if candles[j]["close"] < sl["price"]:
                for k in range(j - 1, max(j - lookback_for_ob, sl["index"]) - 1, -1):
                    if candles[k]["close"] > candles[k]["open"]:
                        after = candles[j + 1:]
                        impulse_low = min((c["low"] for c in candles[j:j + 3]), default=candles[j]["low"])
                        ob = {
                            "type": "bearish", "time": candles[k]["time"],
                            "high": candles[k]["high"], "low": candles[k]["low"],
                            "broke_structure_at": candles[j]["time"],
                            "touched": any(c["high"] >= candles[k]["low"] for c in after),
                            "invalidated": any(c["close"] > candles[k]["high"] for c in after),
                            "_end_index": k,
                            "_size_atr": (candles[k]["high"] - candles[k]["low"]) / atr_value if atr_value else None,
                            "_disp_atr": (candles[k]["close"] - impulse_low) / atr_value if atr_value else None,
                        }
                        obs.append(ob)
                        break
                break

    fresh = [o for o in obs if not o["invalidated"]]
    deduped = []
    seen = set()
    for o in fresh:
        key = (o["type"], o["_end_index"])
        if key in seen:
            continue
        seen.add(key)
        age = len(candles) - 1 - o["_end_index"]
        o["age_candles"] = age
        o["relevance_score"] = _relevance_score(o["_size_atr"], o["_disp_atr"], age, len(candles))
        del o["_end_index"], o["_size_atr"], o["_disp_atr"]
        deduped.append(o)
    deduped.sort(key=lambda o: o["relevance_score"], reverse=True)
    return deduped[:max_results]


# ------------------------------------------------------------------ fvgs --

def detect_fvgs(candles, atr_value=None, max_results=8):
    """3-candle imbalance. Bullish: candle[i-1].high < candle[i+1].low.
    Bearish: candle[i-1].low > candle[i+1].high.

    `partially_filled` = price has traded into the near edge; `filled`
    (fully filled) = price has traded through to the far edge. Only
    non-fully-filled ones are returned, sorted by relevance_score."""
    fvgs = []
    for i in range(1, len(candles) - 1):
        c1, c2, c3 = candles[i - 1], candles[i], candles[i + 1]
        after = candles[i + 2:]
        if c1["high"] < c3["low"]:
            zone_bottom, zone_top = c1["high"], c3["low"]
            fvgs.append({
                "type": "bullish", "time": c2["time"], "top": zone_top, "bottom": zone_bottom,
                "partially_filled": any(c["low"] <= zone_top for c in after),
                "filled": any(c["low"] <= zone_bottom for c in after),
                "_index": i,
                "_size_atr": (zone_top - zone_bottom) / atr_value if atr_value else None,
                "_disp_atr": (c2["close"] - c2["open"]) / atr_value if atr_value else None,
            })
        elif c1["low"] > c3["high"]:
            zone_bottom, zone_top = c3["high"], c1["low"]
            fvgs.append({
                "type": "bearish", "time": c2["time"], "top": zone_top, "bottom": zone_bottom,
                "partially_filled": any(c["high"] >= zone_bottom for c in after),
                "filled": any(c["high"] >= zone_top for c in after),
                "_index": i,
                "_size_atr": (zone_top - zone_bottom) / atr_value if atr_value else None,
                "_disp_atr": (c2["open"] - c2["close"]) / atr_value if atr_value else None,
            })

    open_fvgs = [f for f in fvgs if not f["filled"]]
    for f in open_fvgs:
        age = len(candles) - 1 - f["_index"]
        f["age_candles"] = age
        f["relevance_score"] = _relevance_score(f["_size_atr"], f["_disp_atr"], age, len(candles))
        del f["_index"], f["_size_atr"], f["_disp_atr"]
    open_fvgs.sort(key=lambda f: f["relevance_score"], reverse=True)
    return open_fvgs[:max_results]


# ------------------------------------------------------------- fib zone --

def golden_zone_from_recent_swing(swings, min_range_pct=1.0):
    """61.8%-78.6% retracement zone off the most recent swing leg large
    enough to matter (filters out tiny noise swings)."""
    if len(swings) < 2:
        return None
    ordered = sorted(swings, key=lambda s: s["index"])
    for a, b in zip(reversed(ordered[:-1]), reversed(ordered[1:])):
        if a["kind"] == b["kind"]:
            continue
        lo, hi = sorted([a["price"], b["price"]])
        if (hi - lo) / lo * 100 < min_range_pct:
            continue
        up_swing = b["kind"] == "high" and b["index"] > a["index"]
        span = hi - lo
        if up_swing:
            zone_top = hi - span * 0.618
            zone_bottom = hi - span * 0.786
        else:
            zone_bottom = lo + span * 0.618
            zone_top = lo + span * 0.786
        return {
            "direction": "up" if up_swing else "down",
            "swing_low": lo, "swing_high": hi,
            "zone_bottom": round(min(zone_bottom, zone_top), 2),
            "zone_top": round(max(zone_bottom, zone_top), 2),
        }
    return None


# ----------------------------------------------------------- liquidity --

def htf_liquidity(d1, w, m):
    out = {}
    if len(d1) >= 2:
        out["prior_day"] = {"high": d1[-1]["high"], "low": d1[-1]["low"]}
    if len(w) >= 2:
        out["prior_week"] = {"high": w[-2]["high"], "low": w[-2]["low"]}
    if len(m) >= 2:
        out["prior_month"] = {"high": m[-2]["high"], "low": m[-2]["low"]}
    return out


# ------------------------------------------------------ premium/discount --

def premium_discount(current_price, swing_low, swing_high):
    if swing_high is None or swing_low is None or swing_high <= swing_low:
        return None
    pct = (current_price - swing_low) / (swing_high - swing_low)
    pct = max(0.0, min(1.0, pct))
    if pct >= 0.618:
        label = "premium"
    elif pct <= 0.382:
        label = "discount"
    else:
        label = "equilibrium"
    return {"pct": round(pct, 3), "label": label}


# --------------------------------------------------------------- gating --

def compute_zone_gate(current_price, atr_value, opposing_zones, veto_atr=0.5):
    """opposing_zones: [{low, high, ...}] — zones that argue AGAINST the
    direction being checked (e.g. bullish zones below price when checking
    a SELL). Returns whether an opposing zone sits within `veto_atr` of
    price (gate='blocked') and how much room exists otherwise, in ATR."""
    if not opposing_zones or not atr_value:
        return {"gate": "clear", "room_atr": None, "nearest_zone": None}
    scored = []
    for z in opposing_zones:
        if z["low"] <= current_price <= z["high"]:
            dist = 0.0
        elif current_price < z["low"]:
            dist = z["low"] - current_price
        else:
            dist = current_price - z["high"]
        scored.append((dist, z))
    scored.sort(key=lambda x: x[0])
    nearest_dist, nearest_zone = scored[0]
    room_atr = round(nearest_dist / atr_value, 2)
    return {
        "gate": "blocked" if room_atr <= veto_atr else "clear",
        "room_atr": room_atr,
        "nearest_zone": nearest_zone,
    }


# ------------------------------------------------------------- features --

def _timeframe_block(candles, atr_value):
    swings = detect_swings(candles, strength=2)
    recent = sorted(swings, key=lambda s: s["index"])[-20:]
    highs = [s["price"] for s in recent if s["kind"] == "high"]
    lows = [s["price"] for s in recent if s["kind"] == "low"]
    return {
        "atr": atr_value,
        "order_blocks": detect_order_blocks(candles, swings, atr_value=atr_value),
        "fvgs": detect_fvgs(candles, atr_value=atr_value),
        "equal_levels": detect_equal_levels(candles, swings),
        "golden_zone": golden_zone_from_recent_swing(swings),
        "swing_range": {"low": min(lows), "high": max(highs)} if highs and lows else None,
    }


def _opposing_zones(direction, d1_block, h4_block):
    """Zones that argue against `direction` ('buy' or 'sell'): unmitigated
    bearish OB/FVG/golden-zone block a BUY; unmitigated bullish ones block
    a SELL."""
    want_type = "bearish" if direction == "buy" else "bullish"
    zones = []
    for block in (d1_block, h4_block):
        for ob in block["order_blocks"]:
            if ob["type"] == want_type:
                zones.append({"low": ob["low"], "high": ob["high"], "kind": "OB"})
        for fvg in block["fvgs"]:
            if fvg["type"] == want_type:
                zones.append({"low": fvg["bottom"], "high": fvg["top"], "kind": "FVG"})
        gz = block["golden_zone"]
        if gz:
            matches = (gz["direction"] == "down" and want_type == "bearish") or \
                      (gz["direction"] == "up" and want_type == "bullish")
            if matches:
                zones.append({"low": gz["zone_bottom"], "high": gz["zone_top"], "kind": "golden_zone"})
    return zones


def compute_htf_features(d1, h4, w, m, current_price):
    """Pure function, no I/O: the full as-of HTF snapshot. Safe to call
    once per timestamp from a backtester by passing candle history sliced
    up to (and not beyond) that timestamp."""
    d1_block = _timeframe_block(d1, atr(d1, 14))
    h4_block = _timeframe_block(h4, atr(h4, 14))

    pd_d1 = premium_discount(current_price, *(d1_block["swing_range"] or {}).values()) if d1_block["swing_range"] else None
    pd_h4 = premium_discount(current_price, *(h4_block["swing_range"] or {}).values()) if h4_block["swing_range"] else None

    gates = {}
    for direction in ("buy", "sell"):
        zones = _opposing_zones(direction, d1_block, h4_block)
        gates[direction] = compute_zone_gate(current_price, d1_block["atr"], zones)

    return {
        "current_price": current_price,
        "liquidity": htf_liquidity(d1, w, m),
        "d1": {**d1_block, "premium_discount": pd_d1},
        "h4": {**h4_block, "premium_discount": pd_h4},
        "gates": gates,
    }


# ---------------------------------------------------------------- print --

def _annotate(label, price_low, price_high, current_price):
    mid = (price_low + price_high) / 2
    dist_pct = round(abs(current_price - mid) / current_price * 100, 3)
    inside = price_low <= current_price <= price_high
    return f"{label}: [{price_low:.2f} - {price_high:.2f}]  dist={dist_pct}%  {'(PRICE INSIDE)' if inside else ''}"


def _print_timeframe(name, block, current_price):
    print(f"\n{'='*70}\n{name}\n{'='*70}")
    print(f"ATR(14): {block['atr']}")

    print("\n-- Order Blocks (unmitigated = not invalidated) --")
    if not block["order_blocks"]:
        print("  none found")
    for ob in block["order_blocks"]:
        flags = ("touched, " if ob["touched"] else "") + f"relevance={ob['relevance_score']}"
        print(f"  {ob['type'].upper():8s} " + _annotate("OB", ob["low"], ob["high"], current_price) +
              f"  formed {ob['time'][:10]}  ({flags})")

    print("\n-- Fair Value Gaps (not fully filled) --")
    if not block["fvgs"]:
        print("  none found")
    for fvg in block["fvgs"]:
        flags = ("partial fill, " if fvg["partially_filled"] else "") + f"relevance={fvg['relevance_score']}"
        print(f"  {fvg['type'].upper():8s} " + _annotate("FVG", fvg["bottom"], fvg["top"], current_price) +
              f"  formed {fvg['time'][:10]}  ({flags})")

    print("\n-- Equal Highs / Equal Lows (liquidity pools) --")
    eqs = block["equal_levels"]
    if not eqs:
        print("  none found")
    for eq in sorted(eqs, key=lambda e: abs(e["price_avg"] - current_price))[:5]:
        dist_pct = round(abs(current_price - eq["price_avg"]) / current_price * 100, 3)
        print(f"  {eq['kind']:12s} ~{eq['price_avg']:.2f}  ({eq['touches']} touches)  dist={dist_pct}%")

    print("\n-- Fibonacci Golden Zone (61.8%-78.6%) --")
    gz = block["golden_zone"]
    if gz:
        print(f"  {gz['direction']}-swing  [{gz['swing_low']:.2f} - {gz['swing_high']:.2f}]  -> "
              + _annotate("golden zone", gz["zone_bottom"], gz["zone_top"], current_price))
    else:
        print("  no qualifying swing found")

    pd = block.get("premium_discount")
    if pd:
        print(f"\n-- Premium/Discount -- {pd['label']} ({pd['pct']*100:.1f}% of recent range)")


def main():
    current_price = get_live_price()
    print(f"XAU_USD live price: {current_price}   ({datetime.now(timezone.utc).isoformat()})")

    d1 = get_candles("D", count=250)
    h4 = get_candles("H4", count=300)
    w = get_candles("W", count=52)
    m = get_candles("M", count=24)

    features = compute_htf_features(d1, h4, w, m, current_price)

    print("\n" + "=" * 70)
    print("LIQUIDITY (prior day/week/month)")
    print("=" * 70)
    for label, lvl in features["liquidity"].items():
        dist_high = round(abs(current_price - lvl["high"]) / current_price * 100, 3)
        dist_low = round(abs(current_price - lvl["low"]) / current_price * 100, 3)
        print(f"  {label:12s} high={lvl['high']:.2f} (dist {dist_high}%)   low={lvl['low']:.2f} (dist {dist_low}%)")

    _print_timeframe("D1", features["d1"], current_price)
    _print_timeframe("H4", features["h4"], current_price)

    print(f"\n{'='*70}\nZONE GATES\n{'='*70}")
    for direction, g in features["gates"].items():
        print(f"  {direction.upper():5s} gate={g['gate']:8s} room_atr={g['room_atr']}"
              + (f"  nearest={g['nearest_zone']['kind']} [{g['nearest_zone']['low']:.2f}-{g['nearest_zone']['high']:.2f}]" if g["nearest_zone"] else ""))


if __name__ == "__main__":
    main()
