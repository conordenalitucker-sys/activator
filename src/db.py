"""Supabase REST data layer for Project Activator.

Reads credentials from the environment; for local runs it loads them from the
project .env. On Streamlit Cloud, app.py populates os.environ from st.secrets
before importing this module.

Python 3.9 compatible.
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent


def _load_local_env() -> None:
    if os.environ.get("SUPABASE_URL"):
        return
    envf = ROOT / ".env"
    if not envf.exists():
        return
    for line in envf.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_local_env()


def _base() -> str:
    return os.environ["SUPABASE_URL"].rstrip("/").removesuffix("/rest/v1")


def _headers(extra=None) -> dict:
    key = os.environ["SUPABASE_SECRET_KEY"]
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def get(path: str):
    r = requests.get(f"{_base()}/rest/v1/{path}", headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def patch(path: str, body: dict):
    r = requests.patch(f"{_base()}/rest/v1/{path}", headers=_headers({"Prefer": "return=representation"}),
                       json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def post(path: str, body, prefer: str = "return=representation"):
    r = requests.post(f"{_base()}/rest/v1/{path}", headers=_headers({"Prefer": prefer}),
                      json=body, timeout=30)
    r.raise_for_status()
    return r.json() if r.text else None


# --- convenience -----------------------------------------------------------

def get_config() -> dict:
    rows = get("config?select=*&id=eq.1")
    return rows[0] if rows else {}


def update_config(fields: dict):
    return patch("config?id=eq.1", fields)


def get_contacts() -> list:
    return get("contacts?select=*&order=manual_priority.desc,name.asc")


def get_companies_map() -> dict:
    return {c["id"]: c["name"] for c in get("companies?select=id,name")}


def get_company_choices() -> list:
    return get("companies?select=id,name&order=name.asc")


def todays_interaction_count(today_iso: str) -> int:
    return len(get(f"interactions?date=eq.{today_iso}&select=id"))


def update_contact(contact_id: str, fields: dict):
    return patch(f"contacts?id=eq.{contact_id}", fields)


def insert_contact(fields: dict):
    return post("contacts", fields)


def ensure_company(name: str) -> str:
    name = name.strip()
    existing = get(f"companies?select=id&name=eq.{requests.utils.quote(name)}")
    if existing:
        return existing[0]["id"]
    created = post("companies", {"name": name})
    return created[0]["id"]


def log_interaction(fields: dict):
    return post("interactions", fields, prefer="return=minimal")


def todays_minutes(today_iso: str) -> int:
    rows = get(f"interactions?date=eq.{today_iso}&select=duration_minutes")
    return sum((r.get("duration_minutes") or 0) for r in rows)


def all_interactions_brief() -> list:
    return get("interactions?select=date,duration_minutes,type,contact_id&order=date.desc")


def get_business() -> list:
    return get("business_origination?select=*&order=date.desc")


def insert_business(fields: dict):
    return post("business_origination", fields)


# --- companies / entities / signals (monitoring) ---------------------------

def get_companies_full() -> list:
    return get("companies?select=*&order=name.asc")


def update_company(company_id: str, fields: dict):
    return patch(f"companies?id=eq.{company_id}", fields)


def merge_companies(source_id: str, target_id: str):
    """Move all references from a duplicate company onto a canonical one, then delete
    the duplicate. Reassigns contacts, entities, and signals."""
    patch(f"contacts?company_id=eq.{source_id}", {"company_id": target_id})
    patch(f"entities?related_company_id=eq.{source_id}", {"related_company_id": target_id})
    patch(f"signals?company_id=eq.{source_id}", {"company_id": target_id})
    import requests as _r
    r = _r.delete(f"{_base()}/rest/v1/companies?id=eq.{source_id}",
                  headers=_headers({"Prefer": "return=minimal"}), timeout=30)
    r.raise_for_status()


def get_entities(company_id: str = None) -> list:
    q = "entities?select=*&order=name.asc"
    if company_id:
        q = f"entities?select=*&related_company_id=eq.{company_id}&order=name.asc"
    return get(q)


def insert_entity(fields: dict):
    return post("entities", fields)


def delete_entity(entity_id: str):
    return patch(f"entities?id=eq.{entity_id}", {"enabled": False})  # soft-disable


def hard_delete_entity(entity_id: str):
    import requests as _r
    r = _r.delete(f"{_base()}/rest/v1/entities?id=eq.{entity_id}",
                  headers=_headers({"Prefer": "return=minimal"}), timeout=30)
    r.raise_for_status()


def get_signals(limit: int = 200, include_dismissed: bool = False) -> list:
    q = (f"signals?select=*&order=event_date.desc.nullslast,created_at.desc&limit={limit}")
    if not include_dismissed:
        q += "&dismissed=eq.false"
    return get(q)


def signal_exists(company_id: str, url: str, title: str) -> bool:
    import requests as _r
    url = (url or "").replace("*", "%2A")
    title = (title or "")[:200]
    params = {"company_id": f"eq.{company_id}", "select": "id", "limit": "1"}
    if url:
        params["url"] = f"eq.{url}"
    else:
        params["title"] = f"eq.{title}"
    r = _r.get(f"{_base()}/rest/v1/signals", headers=_headers(), params=params, timeout=30)
    r.raise_for_status()
    return len(r.json()) > 0


def insert_signals(rows: list):
    if not rows:
        return None
    return post("signals", rows, prefer="return=minimal")


def dismiss_signal(signal_id: str):
    return patch(f"signals?id=eq.{signal_id}", {"dismissed": True})
