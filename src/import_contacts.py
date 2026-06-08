"""Import contacts from the BizDev Excel into Supabase.

Reads the "Contacts" sheet, dedupes Organizations into `companies`, maps each
contact row, and seeds manual_priority + cadence_tier from the Green/Blue/Purple
priority color. Idempotent-ish: skips contacts whose (name, company) already exist.

Python 3.9 compatible. Stdlib + openpyxl only.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, date
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_XLSX = "/Users/CTuckerPersonal/Desktop/Activator-Project/C-Tucker_BizDevNotes(correct).xlsx"

# Green/Blue/Purple -> (manual_priority, cadence_tier)
COLOR_MAP = {
    "green": (5, "monthly"),
    "blue": (3, "bimonthly"),
    "purple": (1, "biannual"),
}

TYPE_MAP = {
    "client": "client",
    "past client": "past-client",
    "past-client": "past-client",
    "prospect": "prospect",
    "professional": "professional",
    "referral": "referral",
    "friend": "friend",
}


def load_env() -> dict:
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    base = env["SUPABASE_URL"].rstrip("/").removesuffix("/rest/v1")
    return {"base": base, "key": env["SUPABASE_SECRET_KEY"]}


def rest(cfg, method, path, body=None, prefer=None):
    url = f"{cfg['base']}/rest/v1/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("apikey", cfg["key"])
    req.add_header("Authorization", f"Bearer {cfg['key']}")
    req.add_header("Content-Type", "application/json")
    if prefer:
        req.add_header("Prefer", prefer)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"HTTP {e.code} on {method} {path}: {e.read().decode()}\n")
        raise


def cell(row, idx, headers):
    i = headers.get(idx)
    if i is None or i >= len(row):
        return None
    v = row[i]
    if isinstance(v, str):
        v = v.strip()
    return v or None


def to_iso(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(v, fmt).date().isoformat()
            except ValueError:
                continue
    return None


def main():
    xlsx = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_XLSX
    cfg = load_env()

    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    ws = wb["Contacts"]
    rows = list(ws.iter_rows(values_only=True))
    raw_headers = [str(c).strip() if c is not None else "" for c in rows[0]]
    H = {h: i for i, h in enumerate(raw_headers)}

    def col(name_options):
        for n in name_options:
            if n in H:
                return n
        return None

    c_city = col(["City"])
    c_name = col(["Name"])
    c_org = col(["Organization"])
    c_email = col(["Email"])
    c_phone = col(["Phone"])
    c_type = col([h for h in raw_headers if h.startswith("Type of Contact")])
    c_prio = col(["Priority Level"])
    c_last = col([h for h in raw_headers if h.startswith("Date of Last Contact")])
    c_info = col(["Information Learned"])

    # --- pass 1: dedupe organizations -> companies ---
    org_names = []
    for r in rows[1:]:
        if not cell(r, c_name, H):
            continue
        org = cell(r, c_org, H)
        if org and org not in org_names:
            org_names.append(org)

    existing = rest(cfg, "GET", "companies?select=id,name")
    company_id = {c["name"].lower(): c["id"] for c in existing}
    new_orgs = [o for o in org_names if o.lower() not in company_id]
    if new_orgs:
        inserted = rest(cfg, "POST", "companies", body=[{"name": o} for o in new_orgs],
                        prefer="return=representation")
        for c in inserted:
            company_id[c["name"].lower()] = c["id"]

    # --- pass 2: contacts ---
    existing_contacts = rest(cfg, "GET", "contacts?select=name,company_id")
    seen = {(c["name"].lower(), c.get("company_id")) for c in existing_contacts}

    payload = []
    skipped_no_name = 0
    for r in rows[1:]:
        name = cell(r, c_name, H)
        if not name:
            skipped_no_name += 1
            continue
        org = cell(r, c_org, H)
        cid = company_id.get(org.lower()) if org else None
        if (name.lower(), cid) in seen:
            continue

        prio = (cell(r, c_prio, H) or "").lower()
        manual_priority, cadence = COLOR_MAP.get(prio, (None, None))
        color = prio.capitalize() if prio in COLOR_MAP else None

        ctype_raw = (cell(r, c_type, H) or "").lower()
        ctype = TYPE_MAP.get(ctype_raw)

        payload.append({
            "name": name,
            "company_id": cid,
            "location": cell(r, c_city, H),
            "email": cell(r, c_email, H),
            "phone": cell(r, c_phone, H),
            "contact_type": ctype,
            "manual_priority": manual_priority,
            "priority_color": color,
            "cadence_tier": cadence,
            "personal_notes": cell(r, c_info, H),
            "last_contacted_at": to_iso(cell(r, c_last, H)),
        })

    if payload:
        rest(cfg, "POST", "contacts", body=payload, prefer="return=minimal")

    print(f"Companies: {len(company_id)} total ({len(new_orgs)} new this run)")
    print(f"Contacts imported this run: {len(payload)}")
    print(f"Rows skipped (no name): {skipped_no_name}")


if __name__ == "__main__":
    main()
