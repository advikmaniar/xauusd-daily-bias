"""
scalp_report.py — parse the "XAUUSD Scalp" calendar event the scalping
routine writes when the Scalping screen's Start button fires it.

Kept free of Flask/Google imports so it can be unit-tested on its own
(see test_scalp_report.py) and reused by any other consumer.

The routine updates ONE event in place (summary "XAUUSD Scalp"), so the
description is the whole report. Format — one item per line, "|" between
fields, sections in this order:

    XAUUSD SCALP
    Updated: 2026-10-20T17:52:00Z
    Price: 4125.32
    Direction: BEARISH | Coiling in an $11 range under the 15m EMAs
    What happened:
    - <point>
    - <point>
    - <point>
    Levels:
    - R | 4127-4128 | 15m EMA20/50 + 16:30 high | best
    - S | 4105-4110 | buy 1.83% | best
    Setups:
    1. SHORT | High | 4126-4128 | 4131 | 4121, 4118 | Range-top fade
    Notes:
    - <point>

The word "bias" must never appear anywhere in the event: the Daily Bias
screen finds its own event with a calendar search for "XAUUSD Bias", which
also matches description text. That's why the line is "Direction:".

Levels: side is R (resistance) or S (support), listed high to low; the
optional last field "best" marks the strongest level on that side.
Setups: rank. DIRECTION | confidence | entry | stop | targets | trigger.
Anything that doesn't parse is skipped rather than failing the whole
report, and the raw text is always returned for a fallback view.
"""

import re

SECTIONS = {
    "what happened": "happened",
    "levels": "levels",
    "setups": "setups",
    "notes": "notes",
}
BIASES = ("BULLISH", "BEARISH", "NEUTRAL")
DIRECTIONS = ("LONG", "SHORT")
CONFIDENCE = ("High", "Medium", "Low")


def _num(s):
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", s or "")
    return float(m.group(0).replace(",", "")) if m else None


def _bullet(line):
    return re.sub(r"^[-•*]\s*", "", line).strip()


def _parse_level(line):
    parts = [p.strip() for p in _bullet(line).split("|")]
    if len(parts) < 3 or parts[0].upper() not in ("R", "S"):
        return None
    nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", parts[1])]
    if not nums:
        return None
    return {
        "side": parts[0].upper(),
        "zone": parts[1],
        "low": min(nums),
        "high": max(nums),
        "note": parts[2],
        "best": len(parts) > 3 and parts[3].lower().startswith("best"),
    }


def _parse_setup(line):
    m = re.match(r"^\s*(\d+)[.)]\s*(.+)$", line)
    if not m:
        return None
    parts = [p.strip() for p in m.group(2).split("|")]
    if len(parts) < 5 or parts[0].upper() not in DIRECTIONS:
        return None
    conf = parts[1].capitalize()
    return {
        "rank": int(m.group(1)),
        "direction": parts[0].upper(),
        "confidence": conf if conf in CONFIDENCE else None,
        "entry": parts[2],
        "stop": parts[3],
        "targets": [t.strip() for t in re.split(r"[,/→]|then", parts[4]) if t.strip()],
        "trigger": parts[5] if len(parts) > 5 else "",
    }


def parse_scalp_report(description):
    """Return a dict for the UI. Keys are always present; unparseable
    pieces come back empty so the UI can fall back to `raw`."""
    desc = (description or "").replace("\r\n", "\n")
    out = {
        "updated": None, "price": None, "bias": None, "bias_note": "",
        "happened": [], "levels": [], "setups": [], "notes": [],
        "raw": desc,
    }
    section = None
    for raw_line in desc.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        key = line.rstrip(":").strip().lower()
        if key in SECTIONS:
            section = SECTIONS[key]
            continue
        low = line.lower()
        if low.startswith("updated:"):
            out["updated"] = line.split(":", 1)[1].strip()
            continue
        if low.startswith("price:"):
            out["price"] = _num(line.split(":", 1)[1])
            continue
        if low.startswith("direction:"):
            bias, _, note = line.split(":", 1)[1].partition("|")
            bias = bias.strip().upper()
            out["bias"] = bias if bias in BIASES else None
            out["bias_note"] = note.strip()
            continue
        if section in ("happened", "notes") and re.match(r"^[-•*]", line):
            out[section].append(_bullet(line))
        elif section == "levels":
            lvl = _parse_level(line)
            if lvl:
                out["levels"].append(lvl)
        elif section == "setups":
            s = _parse_setup(line)
            if s:
                out["setups"].append(s)

    # Caps from the spec: 3 / 4–8 / top 5 / 3. Levels high→low, setups by rank.
    out["happened"] = out["happened"][:3]
    out["notes"] = out["notes"][:3]
    out["levels"] = sorted(out["levels"], key=lambda l: -l["high"])[:8]
    out["setups"] = sorted(out["setups"], key=lambda s: s["rank"])[:5]
    return out


def is_usable(report):
    """Enough parsed to render the structured view (else show raw)."""
    return bool(report["bias"] and (report["levels"] or report["setups"]))


# Example report in the exact format above — used by MOCK_MODE, the tests,
# and as the format reference in the routine prompt.
SAMPLE_REPORT = """XAUUSD SCALP
Updated: 2026-10-20T17:52:00Z
Price: 4125.32
Direction: BEARISH | Coiling in an $11 range under the 15m EMAs
What happened:
- S1 pivot (4121.5) dipped to 4117.4 at 17:00 and was reclaimed; price back to 4123-4125
- Last hour compressed into a 4117.4-4128.1 range — 5m ATR down to $2.6, breakout likely later in the US afternoon
- Highs still stepping lower (4128.1 → 4125.5 → 4124.9) and price sits under the 15m EMA20/50 (4127.3 / 4127.9)
Levels:
- R | 4132-4136 | Lower highs + buy-stops 0.58% (squeeze fuel above 4128) |
- R | 4127-4128 | 15m EMA20/50 + 16:30 high (4128.1) | best
- R | 4125-4126 | Recent lower highs + 5m EMA20 |
- S | 4120-4121.5 | S1 pivot (4121.5) + 16:45 low |
- S | 4117-4118 | 17:00 low (4117.4) = range floor |
- S | 4110-4115 | Day low (4111.7) + buy 1.33% |
- S | 4105-4110 | Buy cluster 1.83% | best
- S | 4100-4105 | S2 pivot (4102.1) + buy 1.37% |
Setups:
1. SHORT | High | 4126-4128 | 4131 | 4121, 4118 | Range-top fade under the 15m EMAs
2. LONG | High | 4106-4109 | 4100 | 4116, 4121 | Main buy cluster
3. SHORT | Medium | 15m close < 4117 | 4122 | 4111, 4106 | Breakdown of the range floor
4. LONG | Medium | 4118-4120 | 4114 | 4124, 4127 | Quick range-floor scalp
5. LONG | Low | 15m close > 4129 | 4124 | 4135, 4140 | Squeeze through buy-stops
Notes:
- $11 range — trade the edges with small targets; the middle (4121-4125) is noise
- Retail 71.7% long: more stop fuel below price, so a downside break is slightly favoured
- US cash close 20:00 UTC — last real impulse window is 18:30-19:30 UTC
"""
