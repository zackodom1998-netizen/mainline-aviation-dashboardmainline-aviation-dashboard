"""
github_action_refresh.py
────────────────────────
Runs inside GitHub Actions (Ubuntu, no Chrome, no Windows).
Logs into Springshot with email + password, fetches 30-day ATL data,
rebuilds the dashboard HTML, and saves it to the repo root so GitHub
Pages picks it up automatically.

Credentials are passed via environment variables (stored as GitHub Secrets):
    SPRINGSHOT_EMAIL
    SPRINGSHOT_PASSWORD
"""

import csv
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ── Paths (relative to repo root, where the Action checks out) ───────────────
REPO_ROOT      = Path(__file__).parent.parent
AUTO_DIR       = REPO_ROOT / "automation"
DASHBOARD_PATH = REPO_ROOT / "Missions_Operations_Dashboard.html"
MASTER_PATH    = REPO_ROOT / "MissionsSummary_master.csv"
BACKUP_DIR     = AUTO_DIR / "backups"

SPRINGSHOT_BASE = "https://webapp.springshot.com"
LOGIN_URL       = f"{SPRINGSHOT_BASE}/authentication/login"
API_PATTERN     = (
    "/CabinCleaningMissions/summaryWidget/5-9-84-10-176-52/487"
    "/refreshed/ajax/summary_widget/292"
    "?airportCode=ATL&startDate={start}&endDate={end}"
    "&jobs=M20-19:A20&missionTypes=5050-442-1009-699-443-4107-448"
)

def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ── Step 1: Login ─────────────────────────────────────────────────────────────
def springshot_login(email: str, password: str) -> requests.Session:
    """
    Log into Springshot and return an authenticated requests.Session.

    Flow:
      1. GET the login page to collect the CSRF token (if any).
      2. POST credentials to the login endpoint.
      3. Verify we have a valid session by probing a protected endpoint.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer":    SPRINGSHOT_BASE,
    })

    # ── 1a. GET login page — grab CSRF token if present ──────────────────────
    log("GET login page …")
    get_resp = session.get(LOGIN_URL, timeout=30)
    csrf_token = None

    # Try standard <input name="_token" value="..."> pattern (Laravel / CakePHP)
    for pattern in [
        r'<input[^>]+name=["\']_token["\'][^>]*value=["\']([^"\']+)["\']',
        r'<input[^>]+value=["\']([^"\']+)["\'][^>]*name=["\']_token["\']',
        r'<meta[^>]+name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']',
        r'"csrfToken"\s*:\s*"([^"]+)"',
        r'csrfToken["\s:=]+["\']([a-zA-Z0-9+/=_-]{20,})["\']',
    ]:
        m = re.search(pattern, get_resp.text, re.I)
        if m:
            csrf_token = m.group(1)
            log(f"CSRF token found ({len(csrf_token)} chars)")
            break

    if not csrf_token:
        log("No CSRF token found — attempting login without one.")

    # ── 1b. POST credentials ──────────────────────────────────────────────────
    payload = {"email": email, "password": password}
    if csrf_token:
        payload["_token"] = csrf_token

    log(f"POST credentials to {LOGIN_URL} …")
    post_resp = session.post(
        LOGIN_URL,
        data=payload,
        headers={
            "Content-Type":  "application/x-www-form-urlencoded",
            "Referer":       LOGIN_URL,
            "X-Requested-With": "XMLHttpRequest",
        },
        allow_redirects=True,
        timeout=30,
    )

    # ── 1c. Verify we landed on the dashboard (not still on login) ────────────
    if "authentication/login" in post_resp.url and post_resp.status_code == 200:
        # Might be a JSON API auth instead — try that
        log("Form POST redirected back to login — trying JSON auth endpoint …")
        json_resp = session.post(
            f"{SPRINGSHOT_BASE}/api/v1/auth/login",
            json={"email": email, "password": password},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=30,
        )
        if json_resp.status_code == 200:
            data = json_resp.json()
            token = data.get("token") or data.get("access_token") or data.get("data", {}).get("token")
            if token:
                session.headers["Authorization"] = f"Bearer {token}"
                log("JSON auth succeeded — using Bearer token.")
                return session
        log(f"JSON auth also failed (HTTP {json_resp.status_code}).")
        log("LOGIN FAILED — check SPRINGSHOT_EMAIL / SPRINGSHOT_PASSWORD secrets.")
        log(f"Final URL: {post_resp.url}  |  Status: {post_resp.status_code}")
        sys.exit(1)

    log(f"Login succeeded (redirected to {post_resp.url})")
    return session


# ── Step 2: Fetch missions ────────────────────────────────────────────────────
def fetch_missions(session: requests.Session):
    end   = datetime.now()
    start = end - timedelta(days=30)
    fmt   = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S")

    url = SPRINGSHOT_BASE + API_PATTERN.format(start=fmt(start), end=fmt(end))
    log(f"Fetching ATL missions {fmt(start)} → {fmt(end)} …")

    resp = session.get(
        url,
        headers={
            "Accept":          "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer":         f"{SPRINGSHOT_BASE}/dashboard",
        },
        timeout=60,
    )

    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        log(f"API call failed: HTTP {resp.status_code}  content-type={resp.headers.get('content-type')}")
        log(f"Response preview: {resp.text[:300]}")
        sys.exit(1)

    missions = resp.json().get("data", [])
    log(f"Fetched {len(missions)} missions.")
    return missions, start, end


# ── Step 3: Build CSV ─────────────────────────────────────────────────────────
CSV_COLUMNS = [
    "Team Lead","Airline","Mission Type","Worksite","Asset",
    "Engagement","Productivity","Inbound Flight","Outbound Flight",
    "Asset Type","Location","Event",
    "Flight Arrival","Mission Assigned","Mission Accepted",
    "Team Arrival","Mission Started","Mission Completed","Flight Departure",
    "Security Search","Details","Mission Notes","Arrival Delay",
]

def _eng(m):
    s = (m.get("STATUS") or "").lower()
    if "complet" in s: return "100%"
    if any(x in s for x in ("cancel","skip","absent")): return "0%"
    if m.get("TEAM_ARRIVED_DATE") and m["TEAM_ARRIVED_DATE"] != "0000-00-00 00:00:00": return "100%"
    return "0%"

def _prod(m):
    v = m.get("OVERALL_SCORE")
    if v is None or v == "": return "N/A"
    try: return str(round(float(v))) + "%"
    except: return "N/A"

def _cell(v):
    if v is None or str(v) in ("","0000-00-00 00:00:00"): return ""
    s = str(v)
    return '"' + s.replace('"','""') + '"' if any(c in s for c in (',','"','\n')) else s

def missions_to_csv(missions):
    lines = [",".join(CSV_COLUMNS)]
    for m in missions:
        lead = (((m.get("LEAD_FIRST_NAME") or "") + " " + (m.get("LEAD_LAST_NAME") or "")).strip())
        sec  = ("-" if not m.get("HAS_SECURITY_SEARCH_TASKS")
                else ("Compliant" if m.get("SECURITY_SEARCH_IS_COMPLIANT") else "Non-compliant"))
        delay = str(round(m["ARRIVING_SEG_DELAY"])) if m.get("ARRIVING_SEG_DELAY") is not None else ""
        row = [
            lead, m.get("AIRLINE_CODE"), m.get("MISSION_TYPE_CODE"), m.get("AIRPORT_CODE"), m.get("TAIL_NUMBER"),
            _eng(m), _prod(m), m.get("ARRIVING_SEG_NUMBER"), m.get("DEPARTING_SEG_NUMBER"), m.get("VESSEL_DESCRIPTION"),
            m.get("AIRPORT_LOCATION"), m.get("EVENT_NAME") or "-",
            m.get("ARRIVAL_DATE_DISPLAY"), m.get("ASSIGNED_DATE_DISPLAY"), m.get("ACCEPTED_DATE_DISPLAY"),
            m.get("TEAM_ARRIVED_DATE_DISPLAY"), m.get("START_DATE_DISPLAY"),
            m.get("COMPLETED_DATE_DISPLAY"), m.get("DEPARTURE_DATE_DISPLAY"),
            sec, m.get("COMMENTS_NUMBER") or "", m.get("COMMENT_TEXT") or "", delay,
        ]
        lines.append(",".join(_cell(c) for c in row))
    return "﻿" + "\n".join(lines)


# ── Step 4: Merge + rebuild (reuses existing rebuild_dashboard.py) ────────────
def merge_and_rebuild(csv_text: str):
    sys.path.insert(0, str(AUTO_DIR))
    import rebuild_dashboard as rd
    rd.TEST_DIR       = REPO_ROOT
    rd.MASTER_PATH    = MASTER_PATH
    rd.DASHBOARD_PATH = DASHBOARD_PATH
    rd.BACKUP_DIR     = BACKUP_DIR

    # Write incoming CSV to a temp file
    tmp_csv = REPO_ROOT / "MissionsSummary_latest.csv"
    tmp_csv.write_text(csv_text, encoding="utf-8")

    added, skipped, total = rd.merge_incoming(tmp_csv)
    tmp_csv.unlink(missing_ok=True)

    master_rows = list(csv.DictReader(open(MASTER_PATH, encoding="utf-8-sig")))
    enriched = rd.enrich(master_rows)
    log(f"Enriched {len(enriched)} master records.")
    rd.rewrite_dashboard(enriched)
    return added, skipped, total


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    log("=" * 60)
    log(f"Springshot GitHub Actions Refresh — {datetime.now():%Y-%m-%d %H:%M UTC}")
    log("=" * 60)

    email    = os.environ.get("SPRINGSHOT_EMAIL", "").strip()
    password = os.environ.get("SPRINGSHOT_PASSWORD", "").strip()

    if not email or not password:
        log("ERROR: SPRINGSHOT_EMAIL and SPRINGSHOT_PASSWORD must be set as GitHub Secrets.")
        log("Go to: repo → Settings → Secrets and variables → Actions → New repository secret")
        sys.exit(1)

    # 1. Login
    session = springshot_login(email, password)

    # 2. Fetch
    missions, start, end = fetch_missions(session)
    non_zero = sum(1 for m in missions if m.get("ARRIVING_SEG_DELAY") and round(m["ARRIVING_SEG_DELAY"]) != 0)

    # 3. Build CSV + merge + rebuild
    csv_text = missions_to_csv(missions)
    added, skipped, total = merge_and_rebuild(csv_text)

    log("=" * 60)
    log(f"DONE — {len(missions)} missions | {non_zero} delayed | +{added} new | {total} master total")
    log(f"Dashboard: {DASHBOARD_PATH}")
    log("=" * 60)


if __name__ == "__main__":
    main()
