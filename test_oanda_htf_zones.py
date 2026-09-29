"""
Unit tests for oanda_htf_zones.py, against synthetic candle sequences
(no network calls). Run directly:

  python test_oanda_htf_zones.py
"""

import os
os.environ.setdefault("OANDA_API_TOKEN", "unused-in-tests")
os.environ.setdefault("OANDA_ACCOUNT_ID", "unused-in-tests")

import oanda_htf_zones as z

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def C(o, h, l, c, i=0):
    return {"time": f"2020-01-{i+1:02d}T00:00:00.000000000Z", "open": o, "high": h, "low": l, "close": c}


def candles_from(spec):
    """spec: list of (open, high, low, close) tuples."""
    return [C(*ohlc, i=i) for i, ohlc in enumerate(spec)]


# ------------------------------------------------------------------ ATR --

def test_atr():
    print("\n-- atr --")
    # 15 flat candles: high-low = 2 every time, prev close = this candle's
    # close so true range is always exactly 2.
    spec = [(100, 101, 99, 100)] * 15
    candles = candles_from(spec)
    val = z.atr(candles, period=14)
    check("flat series -> ATR == 2.0", val == 2.0, f"got {val}")

    check("too few candles -> None", z.atr(candles[:5], period=14) is None)


# --------------------------------------------------------------- swings --

def test_detect_swings():
    print("\n-- detect_swings --")
    # index 5 is a clean swing high (110), index 10 a clean swing low (90)
    spec = [(100, 101, 99, 100)] * 3
    spec += [(100, 105, 99, 104), (104, 108, 103, 107), (107, 110, 106, 109)]  # 3,4,5 ramp up to swing high at 5
    spec += [(109, 109, 104, 105), (105, 103, 98, 100)]  # 6,7 coming down
    spec += [(100, 101, 95, 96), (96, 97, 92, 93), (93, 94, 90, 91)]  # 8,9,10 ramp down to swing low at 10
    spec += [(91, 96, 90, 95), (95, 99, 94, 98), (98, 100, 96, 99)]  # 11,12,13 back up
    candles = candles_from(spec)
    swings = z.detect_swings(candles, strength=2)
    highs = {s["index"] for s in swings if s["kind"] == "high"}
    lows = {s["index"] for s in swings if s["kind"] == "low"}
    check("swing high detected at index 5", 5 in highs, f"highs={highs}")
    check("swing low detected at index 10", 10 in lows, f"lows={lows}")


def test_detect_equal_levels_and_sweep():
    print("\n-- detect_equal_levels --")
    # two swing highs at ~110 (indices 2 and 7), never swept afterward
    spec = [
        (100, 101, 99, 100), (100, 105, 99, 104), (104, 110, 103, 108),   # idx2 swing high 110
        (108, 105, 100, 101), (101, 103, 98, 100),
        (100, 104, 99, 103), (103, 108, 102, 106),
        (106, 110.05, 104, 108),                                          # idx7 swing high ~110
        (108, 104, 100, 101), (101, 102, 97, 98),
    ]
    candles = candles_from(spec)
    swings = z.detect_swings(candles, strength=2)
    eqs = z.detect_equal_levels(candles, swings, tolerance_pct=0.2)
    equal_highs = [e for e in eqs if e["kind"] == "equal_highs"]
    check("finds an equal-highs cluster with 2 touches", any(e["touches"] == 2 for e in equal_highs), f"{equal_highs}")

    # now add a candle that sweeps above the cluster -> should disappear
    swept_spec = spec + [(98, 115, 97, 112)]
    swept_candles = candles_from(swept_spec)
    swept_swings = z.detect_swings(swept_candles, strength=2)
    swept_eqs = z.detect_equal_levels(swept_candles, swept_swings, tolerance_pct=0.2)
    swept_equal_highs = [e for e in swept_eqs if e["kind"] == "equal_highs"]
    check("swept equal-highs cluster is excluded", len(swept_equal_highs) == 0, f"{swept_equal_highs}")


# ---------------------------------------------------------- order blocks --

def test_order_blocks_bullish():
    print("\n-- detect_order_blocks (bullish) --")
    # Build: chop to create a swing high at 100 (idx 4), then a down candle
    # (the OB, idx 6), then a strong impulse closing above 100 (idx 7).
    spec = [
        (90, 92, 89, 91), (91, 95, 90, 94), (94, 98, 93, 97),
        (97, 100, 96, 99), (99, 100.5, 97, 98),   # idx4: swing high ~100.5
        (98, 99, 95, 96), (96, 97, 93, 94),        # idx5,6: down candles (idx6 = the OB candle, close<open)
        (94, 103, 94, 102),                        # idx7: impulse closes above swing high -> BOS
        (102, 104, 101, 103), (103, 106, 102, 105),
    ]
    candles = candles_from(spec)
    swings = z.detect_swings(candles, strength=2)
    obs = z.detect_order_blocks(candles, swings, atr_value=2.0)
    bullish = [o for o in obs if o["type"] == "bullish"]
    check("finds a bullish OB", len(bullish) >= 1, f"{obs}")
    if bullish:
        ob = bullish[0]
        check("OB candle is index 6 (high=97, low=93)", ob["high"] == 97 and ob["low"] == 93, f"{ob}")
        check("OB not yet touched", ob["touched"] is False, f"{ob}")
        check("OB not invalidated", ob["invalidated"] is False, f"{ob}")
        check("relevance_score is a float in a sane range", 0 <= ob["relevance_score"] <= 2, f"{ob}")

    # extend: price comes back and wicks into the OB range (touch) but
    # closes above its low -> touched=True, invalidated=False, still returned
    touch_spec = spec + [(105, 106, 94, 96)]  # low=94 dips into [93,97], close=96 > low(93)
    touch_candles = candles_from(touch_spec)
    touch_swings = z.detect_swings(touch_candles, strength=2)
    touch_obs = z.detect_order_blocks(touch_candles, touch_swings, atr_value=2.0)
    touch_bullish = [o for o in touch_obs if o["type"] == "bullish"]
    check("touched OB still present (not invalidated)", len(touch_bullish) >= 1, f"{touch_obs}")
    if touch_bullish:
        check("touched flag now True", touch_bullish[0]["touched"] is True, f"{touch_bullish[0]}")

    # extend further: a candle closes below the OB low (93) -> invalidated,
    # must be excluded from the returned (unmitigated) list
    break_spec = touch_spec + [(96, 96, 88, 90)]  # closes at 90 < OB low 93
    break_candles = candles_from(break_spec)
    break_swings = z.detect_swings(break_candles, strength=2)
    break_obs = z.detect_order_blocks(break_candles, break_swings, atr_value=2.0)
    break_bullish = [o for o in break_obs if o["type"] == "bullish" and o["high"] == 97 and o["low"] == 93]
    check("invalidated OB excluded from results", len(break_bullish) == 0, f"{break_obs}")


# ------------------------------------------------------------------ fvgs --

def test_fvgs():
    print("\n-- detect_fvgs --")
    # candle0 high=100, candle1 (big up move), candle2 low=105 -> bullish FVG [100,105]
    spec = [
        (98, 100, 97, 99),
        (99, 108, 99, 107),
        (107, 110, 105, 109),
    ]
    candles = candles_from(spec)
    fvgs = z.detect_fvgs(candles, atr_value=3.0)
    bullish = [f for f in fvgs if f["type"] == "bullish"]
    check("finds the bullish FVG with correct bounds", any(f["bottom"] == 100 and f["top"] == 105 for f in bullish), f"{fvgs}")

    # add a candle that dips to 103 (into the gap, doesn't reach 100) -> partially filled, not filled
    partial_spec = spec + [(109, 109, 103, 106)]
    partial_candles = candles_from(partial_spec)
    partial_fvgs = z.detect_fvgs(partial_candles, atr_value=3.0)
    match = [f for f in partial_fvgs if f["type"] == "bullish" and f["bottom"] == 100 and f["top"] == 105]
    check("partial fill flagged, still open", len(match) == 1 and match[0]["partially_filled"] and not match[0]["filled"], f"{match}")

    # add a candle trading down to 99 (through the whole gap) -> fully filled, excluded
    full_spec = partial_spec + [(106, 107, 99, 101)]
    full_candles = candles_from(full_spec)
    full_fvgs = z.detect_fvgs(full_candles, atr_value=3.0)
    full_match = [f for f in full_fvgs if f["type"] == "bullish" and f["bottom"] == 100 and f["top"] == 105]
    check("fully filled FVG excluded from open list", len(full_match) == 0, f"{full_fvgs}")


# ------------------------------------------------------------- fib zone --

def test_golden_zone():
    print("\n-- golden_zone_from_recent_swing --")
    swings = [
        {"index": 0, "time": "t0", "price": 100.0, "kind": "low"},
        {"index": 5, "time": "t5", "price": 200.0, "kind": "high"},
    ]
    gz = z.golden_zone_from_recent_swing(swings, min_range_pct=1.0)
    check("direction is up", gz["direction"] == "up", f"{gz}")
    expected_top = round(200 - 100 * 0.618, 2)
    expected_bottom = round(200 - 100 * 0.786, 2)
    check("zone bounds match manual 61.8/78.6 calc",
          abs(gz["zone_top"] - expected_top) < 0.01 and abs(gz["zone_bottom"] - expected_bottom) < 0.01,
          f"{gz} vs expected top={expected_top} bottom={expected_bottom}")

    small = [
        {"index": 0, "time": "t0", "price": 100.0, "kind": "low"},
        {"index": 5, "time": "t5", "price": 100.5, "kind": "high"},
    ]
    check("tiny swing below min_range_pct is rejected", z.golden_zone_from_recent_swing(small, min_range_pct=1.0) is None)


# ------------------------------------------------------ premium/discount --

def test_premium_discount():
    print("\n-- premium_discount --")
    check("bottom of range -> discount", z.premium_discount(100, 100, 200)["label"] == "discount")
    check("top of range -> premium", z.premium_discount(200, 100, 200)["label"] == "premium")
    check("middle of range -> equilibrium", z.premium_discount(150, 100, 200)["label"] == "equilibrium")
    check("above range clamps to premium", z.premium_discount(250, 100, 200)["pct"] == 1.0)
    check("degenerate range returns None", z.premium_discount(150, 100, 100) is None)


# --------------------------------------------------------------- gating --

def test_zone_gate():
    print("\n-- compute_zone_gate --")
    zones = [{"low": 101, "high": 102, "kind": "OB"}]
    near = z.compute_zone_gate(current_price=100, atr_value=2.0, opposing_zones=zones, veto_atr=0.5)
    check("zone within veto range -> blocked", near["gate"] == "blocked", f"{near}")
    check("room_atr computed correctly (1pt / 2 ATR = 0.5)", near["room_atr"] == 0.5, f"{near}")

    far_zones = [{"low": 110, "high": 111, "kind": "OB"}]
    far = z.compute_zone_gate(current_price=100, atr_value=2.0, opposing_zones=far_zones, veto_atr=0.5)
    check("distant zone -> clear", far["gate"] == "clear", f"{far}")

    inside_zones = [{"low": 99, "high": 101, "kind": "OB"}]
    inside = z.compute_zone_gate(current_price=100, atr_value=2.0, opposing_zones=inside_zones)
    check("price inside a zone -> distance 0 -> blocked", inside["room_atr"] == 0.0 and inside["gate"] == "blocked", f"{inside}")

    empty = z.compute_zone_gate(current_price=100, atr_value=2.0, opposing_zones=[])
    check("no opposing zones -> clear, room_atr None", empty["gate"] == "clear" and empty["room_atr"] is None, f"{empty}")


# ---------------------------------------------------------- smoke test --

def test_compute_htf_features_smoke():
    print("\n-- compute_htf_features (smoke test) --")
    import random
    random.seed(42)
    price = 2000.0
    d1, h4 = [], []
    for i in range(120):
        price += random.uniform(-15, 15)
        o = price
        h = o + random.uniform(0, 8)
        l = o - random.uniform(0, 8)
        c = o + random.uniform(-5, 5)
        d1.append(C(o, h, l, c, i))
    price2 = 2000.0
    for i in range(200):
        price2 += random.uniform(-6, 6)
        o = price2
        h = o + random.uniform(0, 4)
        l = o - random.uniform(0, 4)
        c = o + random.uniform(-2, 2)
        h4.append(C(o, h, l, c, i))
    w = d1[::5][-52:] or d1[:2]
    m = d1[::20][-24:] or d1[:2]

    features = z.compute_htf_features(d1, h4, w, m, current_price=price)
    check("has top-level keys", set(features.keys()) == {"current_price", "liquidity", "d1", "h4", "gates"}, f"{features.keys()}")
    check("gates has buy and sell", set(features["gates"].keys()) == {"buy", "sell"})
    for direction in ("buy", "sell"):
        g = features["gates"][direction]
        check(f"{direction} gate is a valid verdict", g["gate"] in ("clear", "blocked"), f"{g}")
    check("d1 block has expected keys",
          {"atr", "order_blocks", "fvgs", "equal_levels", "golden_zone", "premium_discount"} <= set(features["d1"].keys()))


def main():
    test_atr()
    test_detect_swings()
    test_detect_equal_levels_and_sweep()
    test_order_blocks_bullish()
    test_fvgs()
    test_golden_zone()
    test_premium_discount()
    test_zone_gate()
    test_compute_htf_features_smoke()

    print(f"\n{'='*50}")
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        raise SystemExit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
