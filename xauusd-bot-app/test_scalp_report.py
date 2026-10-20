"""
Unit tests for scalp_report.py (no network, no Flask). Run directly:

  python test_scalp_report.py
"""

import scalp_report as s

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def test_sample():
    print("\n-- sample report --")
    r = s.parse_scalp_report(s.SAMPLE_REPORT)
    check("header", r["price"] == 4125.32 and r["updated"] == "2026-10-20T17:52:00Z", r)
    check("bias + note", r["bias"] == "BEARISH" and r["bias_note"].startswith("Coiling"), r["bias"])
    check("3 happened / 3 notes", len(r["happened"]) == 3 and len(r["notes"]) == 3)
    check("8 levels high→low", [l["high"] for l in r["levels"]] == sorted([l["high"] for l in r["levels"]], reverse=True)
          and len(r["levels"]) == 8, r["levels"])
    best = [(l["side"], l["zone"]) for l in r["levels"] if l["best"]]
    check("best flags", best == [("R", "4127-4128"), ("S", "4105-4110")], best)
    first = r["setups"][0]
    check("setup parsed", first["direction"] == "SHORT" and first["confidence"] == "High"
          and first["targets"] == ["4121", "4118"] and first["stop"] == "4131", first)
    check("conditional entry kept as text", r["setups"][2]["entry"] == "15m close < 4117")
    check("usable", s.is_usable(r))


def test_caps_and_order():
    print("\n-- caps / ordering --")
    desc = "Direction: BULLISH\nWhat happened:\n" + "".join(f"- p{i}\n" for i in range(6)) + "Setups:\n" + \
        "".join(f"{7 - i}. LONG | low | 1 | 0 | 2 | t\n" for i in range(7))
    r = s.parse_scalp_report(desc)
    check("happened capped at 3", r["happened"] == ["p0", "p1", "p2"], r["happened"])
    check("top 5 by rank", [x["rank"] for x in r["setups"]] == [1, 2, 3, 4, 5], [x["rank"] for x in r["setups"]])
    check("confidence normalised", r["setups"][0]["confidence"] == "Low")


def test_garbage():
    print("\n-- garbage in --")
    r = s.parse_scalp_report("just some text\nLevels:\n- X | nope | n\nSetups:\n1. SIDEWAYS | a | b | c | d")
    check("nothing structured", not r["levels"] and not r["setups"] and r["bias"] is None)
    check("not usable -> raw fallback", not s.is_usable(r) and r["raw"].startswith("just"))
    check("None description", s.parse_scalp_report(None)["raw"] == "")


if __name__ == "__main__":
    test_sample()
    test_caps_and_order()
    test_garbage()
    print(f"\n{'ALL PASSED' if not FAILURES else f'{len(FAILURES)} FAILED: {FAILURES}'}")
    raise SystemExit(1 if FAILURES else 0)
