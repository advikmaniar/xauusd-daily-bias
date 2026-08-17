"""
XAUUSD Bias Bot - local dashboard

Flow:
  1. Browser clicks "Start Bot" -> POST /api/start
  2. Backend fires the Claude Code routine via its API trigger
  3. Browser polls GET /api/status every few seconds
  4. Backend checks Google Calendar for a new "XAUUSD Bias" event created
     after the fire time, and returns it once found
  5. On first "done" response, the call is logged (once) to the "Gold Bias
     Log" Google Sheet for later accuracy grading — see GOOGLE_SHEET_ID

Run:
  pip install flask requests google-auth google-auth-oauthlib --break-system-packages
  python app.py
  open http://localhost:5000
"""

import os
import re
import time
import requests
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template, request

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build

app = Flask(__name__)

# ---- Config (all from environment variables — never hardcode secrets) ----
# Two separate routines: V1 runs oanda_xauusd_report.py (original engine),
# V2 runs oanda_xauusd_report_v2.py (liquidity/fib confluence engine). Each
# is its own Claude Code routine with its own API trigger token — generated
# from that routine's "Add another trigger -> API -> Generate token".
ROUTINES = {
    "v1": {
        "fire_url": os.environ.get("ROUTINE_V1_FIRE_URL"),
        "bearer_token": os.environ.get("ROUTINE_V1_BEARER_TOKEN"),
    },
    "v2": {
        "fire_url": os.environ.get("ROUTINE_V2_FIRE_URL"),
        "bearer_token": os.environ.get("ROUTINE_V2_BEARER_TOKEN"),
    },
}

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")  # "Gold Bias Log" — accuracy tracker

# Local dev/test helpers
MOCK_MODE = os.environ.get("MOCK_MODE", "0") in ("1", "true", "True")

# In-memory run tracker (fine for a single-user local app; swap for a real
# store if you ever deploy this beyond your own machine)
LAST_RUN = {"fired_at": None, "session_url": None, "version": None, "logged": False}


def _google_creds():
    creds = Credentials(
        None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(GoogleAuthRequest())
    return creds


def get_calendar_service():
    return build("calendar", "v3", credentials=_google_creds())


def get_sheets_service():
    return build("sheets", "v4", credentials=_google_creds())


def parse_report_summary(description):
    description = description or ""
    bias_m = re.search(r"BIAS:\s*(BUY|SELL|NONE)", description, re.I)
    conf_m = re.search(r"Confidence:\s*([A-Za-z]+)", description, re.I)
    price_m = re.search(r"Price:\s*\$?([\d,]+(?:\.\d+)?)", description, re.I)
    inval_m = re.search(r"Invalidation:\s*(.+)", description, re.I)
    return {
        "bias": bias_m.group(1).upper() if bias_m else None,
        "confidence": conf_m.group(1) if conf_m else None,
        "price": float(price_m.group(1).replace(",", "")) if price_m else None,
        "invalidation": inval_m.group(1).strip() if inval_m else None,
    }


def log_bias_call(version, description, session_url):
    """Append a row to the Gold Bias Log sheet. Best-effort — a logging
    failure shouldn't break the actual status response the UI depends on."""
    if not GOOGLE_SHEET_ID:
        return
    try:
        summary = parse_report_summary(description)
        row = [[
            datetime.now(timezone.utc).isoformat(),
            version,
            summary["bias"] or "",
            summary["confidence"] or "",
            summary["price"] if summary["price"] is not None else "",
            summary["invalidation"] or "",
            session_url or "",
            "",  # Result — filled in later by the grading routine
        ]]
        get_sheets_service().spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="Sheet1!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": row},
        ).execute()
    except Exception as e:
        print(f"[log_bias_call] failed to log to sheet: {e}")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def start_bot():
    body = request.get_json(silent=True) or {}
    version = body.get("version", "v1")
    if version not in ROUTINES:
        return jsonify({"status": "error", "detail": f"Unknown routine version '{version}'."}), 400

    fired_at = datetime.now(timezone.utc)

    # Mock mode: simulate firing and immediately create an in-memory calendar event
    if MOCK_MODE:
        LAST_RUN["fired_at"] = fired_at.isoformat()
        LAST_RUN["version"] = version
        LAST_RUN["logged"] = False
        LAST_RUN["session_url"] = "https://mock.session.local/session/123"
        # store a mock event created at fired_at
        LAST_RUN["mock_event"] = {
            "created": fired_at.isoformat(),
            "summary": "XAUUSD Bias – SELL",
            "description": (
                "XAUUSD BIAS — Tuesday, August 11, 2026\n"
                "Price: 4368.80 | vs SMA50: above by 220.3 | vs SMA100: below by 20.4\n"
                "Timeframe alignment: intraday fully_aligned_bearish (H1/M15/M5) vs "
                "structural fully_aligned_bullish (D1/H4) — reversal_against_structure=true\n"
                "Liquidity zones: PDL 4365.00 | PDH 4429.09 | PSL 4396.20 | PSH 4432.00 | "
                "PWL 4353.00 | PWH 4429.09\n\n"
                "Key pointers (max 4, only if genuinely impactful):\n"
                "- Reversal against structure: D1/H4 remain bullish (RSI 67.9/51.4) but "
                "H1/M15/M5 are fully aligned bearish — price is -1.49% off the $4435.24 "
                "high set 19h ago; today's bias follows this intraday pullback, not the "
                "structural trend.\n"
                "- No major US data today — CPI is tomorrow (Wed Aug 12) and PPI/jobless "
                "claims Thursday (Aug 14) — so no scheduled catalyst should flip direction "
                "intraday, but positioning ahead of CPI favors caution.\n"
                "- US-Iran military tensions remain elevated (US strikes, Iranian "
                "retaliation on Gulf bases), keeping a geopolitical safe-haven bid under "
                "gold and raising headline-reversal risk even as diplomatic signals "
                "(Rubio) cap urgency.\n"
                "- DXY is firm near 99.8 ahead of Wednesday's CPI, a mild headwind for "
                "gold; price is still trading just below the D1 SMA100 (4389) after "
                "failing to reclaim it.\n\n"
                "BIAS: SELL\n"
                "Confidence: Medium\n"
                "Why: Intraday timeframes are fully aligned bearish in a pullback from "
                "the recent high while structural trend stays bullish, and with no "
                "scheduled US data today to disrupt it, price action should keep "
                "favoring the downside within the range.\n"
                "Invalidation: A reclaim and hold above the H1 SMA20 (~4382) with M15 "
                "structure flipping bullish, or a sudden Iran de-escalation/dollar-selloff "
                "headline driving price back above the $4400 area, would invalidate the "
                "bearish bias."
            ),
            "start": {"dateTime": fired_at.isoformat()},
        }
        return jsonify({
            "status": "started",
            "fired_at": LAST_RUN["fired_at"],
            "session_url": LAST_RUN["session_url"],
            "version": version,
        })

    routine = ROUTINES[version]
    if not routine["fire_url"] or not routine["bearer_token"]:
        return jsonify({
            "status": "error",
            "detail": f"Routine '{version}' isn't configured — missing ROUTINE_{version.upper()}_FIRE_URL / _BEARER_TOKEN.",
        }), 500

    try:
        resp = requests.post(
            routine["fire_url"],
            headers={
                "Authorization": f"Bearer {routine['bearer_token']}",
                "anthropic-beta": "experimental-cc-routine-2026-04-01",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={"text": "Run today's XAUUSD daily bias analysis."},
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        return jsonify({"status": "error", "detail": str(e)}), 502

    if resp.status_code != 200:
        return jsonify({"status": "error", "detail": resp.text}), 502

    data = resp.json()
    LAST_RUN["fired_at"] = fired_at.isoformat()
    LAST_RUN["version"] = version
    LAST_RUN["logged"] = False
    LAST_RUN["session_url"] = data.get("claude_code_session_url")

    return jsonify({
        "status": "started",
        "fired_at": LAST_RUN["fired_at"],
        "session_url": LAST_RUN["session_url"],
        "version": version,
    })


@app.route("/api/status")
def status():
    if not LAST_RUN["fired_at"]:
        return jsonify({"status": "idle"})

    # In mock mode, return the in-memory mock event immediately
    if MOCK_MODE and LAST_RUN.get("mock_event"):
        ev = LAST_RUN["mock_event"]
        return jsonify({
            "status": "done",
            "title": ev.get("summary"),
            "description": ev.get("description"),
            "start": ev.get("start"),
            "session_url": LAST_RUN.get("session_url"),
            "version": LAST_RUN.get("version"),
        })

    service = get_calendar_service()

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    events_result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=today_start.isoformat(),
        q="XAUUSD Bias",
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    events = events_result.get("items", [])
    fired_at_dt = datetime.fromisoformat(LAST_RUN["fired_at"])

    for ev in events:
        created = ev.get("created")
        if not created:
            continue
        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if created_dt >= fired_at_dt:
            if not LAST_RUN.get("logged"):
                log_bias_call(LAST_RUN.get("version"), ev.get("description"), LAST_RUN.get("session_url"))
                LAST_RUN["logged"] = True
            return jsonify({
                "status": "done",
                "title": ev.get("summary"),
                "description": ev.get("description"),
                "start": ev.get("start"),
                "session_url": LAST_RUN["session_url"],
                "version": LAST_RUN.get("version"),
            })

    # Still waiting on the routine to finish and create the event
    elapsed = (datetime.now(timezone.utc) - fired_at_dt).total_seconds()
    timed_out = elapsed > 300  # 5 minutes

    return jsonify({
        "status": "timeout" if timed_out else "pending",
        "elapsed_seconds": int(elapsed),
        "session_url": LAST_RUN["session_url"],
        "version": LAST_RUN.get("version"),
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
