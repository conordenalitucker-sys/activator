"""Mirror Supabase contacts to a Google Sheet you can view anywhere.

Source of truth is Supabase; this pushes a familiar, spreadsheet-style view that
mirrors the original Excel columns (plus cadence). Run on demand or on a schedule.
Two-way edit-back can be layered on later.

Setup (one-time): place the service-account JSON at
  secrets/google-service-account.json
and put the Google Sheet URL in secrets/SHEET.txt . Share the sheet with the service
account's email (Editor).

Python 3.9 compatible.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

ROOT = Path(__file__).resolve().parent.parent
SA_FILE = ROOT / "secrets" / "google-service-account.json"
SHEET_FILE = ROOT / "secrets" / "SHEET.txt"
WORKSHEET = "Contacts"

HEADERS = [
    "City", "Name", "Organization", "Email", "Phone", "Type of Contact",
    "Priority", "Cadence", "Last Contact", "Information Learned",
]


def load_env() -> dict:
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    base = env["SUPABASE_URL"].rstrip("/").removesuffix("/rest/v1")
    return {"base": base, "key": env["SUPABASE_SECRET_KEY"]}


def supa_get(cfg, path):
    req = urllib.request.Request(f"{cfg['base']}/rest/v1/{path}")
    req.add_header("apikey", cfg["key"])
    req.add_header("Authorization", f"Bearer {cfg['key']}")
    return json.loads(urllib.request.urlopen(req).read())


def sheet_id_from_url(url: str) -> str:
    m = re.search(r"/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        raise SystemExit("Could not parse a sheet ID from secrets/SHEET.txt")
    return m.group(1)


def main():
    cfg = load_env()
    contacts = supa_get(cfg, "contacts?select=*&order=manual_priority.desc,name.asc")
    companies = {c["id"]: c["name"] for c in supa_get(cfg, "companies?select=id,name")}

    rows = [HEADERS]
    for c in contacts:
        rows.append([
            c.get("location") or "",
            c.get("name") or "",
            companies.get(c.get("company_id"), "") if c.get("company_id") else "",
            c.get("email") or "",
            c.get("phone") or "",
            c.get("contact_type") or "",
            c.get("priority_color") or "",
            c.get("cadence_tier") or "",
            (c.get("last_contacted_at") or "")[:10],
            c.get("personal_notes") or "",
        ])

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(str(SA_FILE), scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id_from_url(SHEET_FILE.read_text().strip()))
    try:
        ws = sh.worksheet(WORKSHEET)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=WORKSHEET, rows=len(rows) + 10, cols=len(HEADERS))
    ws.clear()
    ws.update(rows, value_input_option="RAW")
    ws.freeze(rows=1)
    print(f"Synced {len(contacts)} contacts to Google Sheet worksheet '{WORKSHEET}'.")


if __name__ == "__main__":
    main()
