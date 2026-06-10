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
import os
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
    "Priority", "Opportunity", "Cadence", "Interests", "Last Contact", "Information Learned",
    "id",  # used to match sheet edits back to the database (don't delete)
]

# Columns the user may edit in the sheet that flow BACK into Supabase.
PRIORITY_COLORS = {"Green", "Blue", "Purple"}
CADENCE_TIERS = {"weekly", "monthly", "bimonthly", "quarterly", "biannual", "annual", "dormant"}


def load_env() -> dict:
    env = {}
    envf = ROOT / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    url = env.get("SUPABASE_URL") or os.environ["SUPABASE_URL"]
    key = env.get("SUPABASE_SECRET_KEY") or os.environ["SUPABASE_SECRET_KEY"]
    base = url.rstrip("/").removesuffix("/rest/v1")
    return {"base": base, "key": key}


def supa_get(cfg, path):
    req = urllib.request.Request(f"{cfg['base']}/rest/v1/{path}")
    req.add_header("apikey", cfg["key"])
    req.add_header("Authorization", f"Bearer {cfg['key']}")
    return json.loads(urllib.request.urlopen(req).read())


def supa_patch(cfg, path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{cfg['base']}/rest/v1/{path}", data=data, method="PATCH")
    req.add_header("apikey", cfg["key"])
    req.add_header("Authorization", f"Bearer {cfg['key']}")
    req.add_header("Content-Type", "application/json")
    urllib.request.urlopen(req).read()


def sheet_id_from_url(url: str) -> str:
    m = re.search(r"/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        raise SystemExit("Could not parse a sheet ID from secrets/SHEET.txt")
    return m.group(1)


def sync_from_sheet(cfg, sh, contacts_by_id):
    """Pull user edits from the Contacts tab back into Supabase. Two-way for a safe set
    of fields: Priority (color), Cadence, Interests, Information Learned. Matches rows by
    the hidden 'id' column; validates enums; only writes fields that actually changed."""
    try:
        ws = sh.worksheet(WORKSHEET)
    except gspread.WorksheetNotFound:
        return 0
    values = ws.get_all_values()
    if len(values) < 2 or "id" not in values[0]:
        return 0
    hdr = {name: i for i, name in enumerate(values[0])}
    if not all(n in hdr for n in ("id", "Priority", "Cadence", "Interests", "Information Learned")):
        return 0

    changed = 0
    for row in values[1:]:
        def cell(name):
            i = hdr[name]
            return row[i].strip() if i < len(row) else ""
        cid = cell("id")
        c = contacts_by_id.get(cid)
        if not cid or not c:
            continue
        upd = {}
        color = cell("Priority")
        if color in PRIORITY_COLORS and color != (c.get("priority_color") or ""):
            upd["priority_color"] = color
        cad = cell("Cadence")
        if cad in CADENCE_TIERS and cad != (c.get("cadence_tier") or ""):
            upd["cadence_tier"] = cad
        interests = [x.strip() for x in cell("Interests").split(",") if x.strip()]
        if interests != (c.get("interests") or []):
            upd["interests"] = interests
        notes = cell("Information Learned")
        if notes != (c.get("personal_notes") or ""):
            upd["personal_notes"] = notes or None
        if upd:
            supa_patch(cfg, f"contacts?id=eq.{cid}", upd)
            changed += 1
    return changed


def main():
    cfg = load_env()
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(str(SA_FILE), scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id_from_url(SHEET_FILE.read_text().strip()))

    # 1) Pull any edits the user made in the sheet back into Supabase FIRST.
    contacts = supa_get(cfg, "contacts?select=*&order=manual_priority.desc,name.asc")
    n = sync_from_sheet(cfg, sh, {c["id"]: c for c in contacts})
    if n:
        print(f"Applied {n} edit(s) from the sheet back to Supabase.")

    # 2) Re-pull (reflecting those edits) and overwrite the sheet from the DB.
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
            c.get("opportunity_score") if c.get("opportunity_score") is not None else "",
            c.get("cadence_tier") or "",
            ", ".join(c.get("interests") or []),
            (c.get("last_contacted_at") or "")[:10],
            c.get("personal_notes") or "",
            c.get("id") or "",
        ])

    def write_tab(title, values):
        try:
            ws = sh.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=title, rows=len(values) + 10, cols=len(values[0]))
        ws.clear()
        ws.update(values, value_input_option="RAW")
        ws.freeze(rows=1)

    write_tab(WORKSHEET, rows)
    print(f"Synced {len(contacts)} contacts to worksheet '{WORKSHEET}'.")

    # --- Signals tab: mirror monitoring signals so everything's in one place ---
    company_names = {c["id"]: c["name"] for c in supa_get(cfg, "companies?select=id,name")}
    entity_names = {e["id"]: e["name"] for e in supa_get(cfg, "entities?select=id,name")}
    sigs = supa_get(cfg, "signals?select=*&dismissed=eq.false"
                         "&order=event_date.desc.nullslast,created_at.desc&limit=500")
    sig_headers = ["Date", "Company", "Entity", "Type", "Score", "Title", "Summary", "Source", "URL"]
    sig_rows = [sig_headers]
    for s in sigs:
        sig_rows.append([
            (s.get("event_date") or "")[:10],
            company_names.get(s.get("company_id"), ""),
            entity_names.get(s.get("entity_id"), "") if s.get("entity_id") else "",
            s.get("type") or "",
            s.get("score_impact") or "",
            s.get("title") or "",
            s.get("summary") or "",
            s.get("source") or "",
            s.get("url") or "",
        ])
    write_tab("Signals", sig_rows)
    print(f"Synced {len(sigs)} signals to worksheet 'Signals'.")


if __name__ == "__main__":
    main()
