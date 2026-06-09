"""Project Activator — dashboard (Streamlit).

Phase 2: Today queue (cadence-driven) + daily progress, Contacts list/filter/edit
with the manual-priority slider, and Add-contact. Opportunity score, monitoring,
and drafting layer on in later phases.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# On Streamlit Cloud, secrets come from st.secrets; mirror them into the env
# so the db layer (which reads os.environ / .env) works everywhere. Locally there
# may be no secrets.toml, so guard the access.
try:
    _secrets = dict(st.secrets)
except Exception:
    _secrets = {}
for _k in ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_PUBLISHABLE_KEY"):
    if _k not in os.environ and _k in _secrets:
        os.environ[_k] = str(_secrets[_k])

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
import db  # noqa: E402

st.set_page_config(page_title="Project Activator", page_icon="📇", layout="wide")

CADENCE_DAYS = {
    "weekly": 7, "monthly": 30, "bimonthly": 60,
    "quarterly": 90, "biannual": 180, "annual": 365, "dormant": None,
}
CADENCE_OPTS = list(CADENCE_DAYS.keys())
COLOR_DOT = {"Green": "🟢", "Blue": "🔵", "Purple": "🟣"}
TYPE_OPTS = ["client", "past-client", "prospect", "professional", "referral", "friend"]
COMM_OPTS = ["email", "call", "LinkedIn", "in-person", "unknown"]
SENIORITY_OPTS = ["decision-maker", "influencer", "staff", "unknown"]
TOUCH_TYPES = ["personal", "pitch", "legal-update", "industry-check", "hello", "other"]
CHANNELS = ["email", "LinkedIn", "call", "in-person", "event", "other"]

TODAY = dt.date.today()
TODAY_ISO = TODAY.isoformat()


# --- cached reads (cleared after writes) -----------------------------------
@st.cache_data(ttl=60)
def load_contacts():
    return db.get_contacts()


@st.cache_data(ttl=60)
def load_companies():
    return db.get_companies_map()


@st.cache_data(ttl=300)
def load_config():
    return db.get_config()


@st.cache_data(ttl=30)
def todays_count():
    return db.todays_interaction_count(TODAY_ISO)


def refresh():
    st.cache_data.clear()


def parse_date(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


def days_since(c):
    d = parse_date(c.get("last_contacted_at"))
    return (TODAY - d).days if d else None


def is_overdue(c):
    interval = CADENCE_DAYS.get(c.get("cadence_tier"))
    if interval is None:
        return False
    ds = days_since(c)
    return ds is None or ds >= interval


def overdue_by(c):
    interval = CADENCE_DAYS.get(c.get("cadence_tier")) or 0
    ds = days_since(c)
    if ds is None:
        return 9999  # never contacted -> top
    return ds - interval


def do_log(contact_id, ttype, channel, minutes, notes, suggested_type=None):
    db.log_interaction({
        "contact_id": contact_id,
        "date": TODAY_ISO,
        "type": ttype,
        "channel": channel,
        "duration_minutes": minutes or None,
        "notes": notes or None,
        "was_suggested": suggested_type is not None,
        "suggested_type": suggested_type,
        "logged_via": "button",
    })
    db.update_contact(contact_id, {"last_contacted_at": TODAY_ISO})
    refresh()


# ---------------------------------------------------------------------------
st.sidebar.title("📇 Project Activator")
page = st.sidebar.radio("Go to", ["Today", "Contacts", "Add contact"])
st.sidebar.caption("Public info + relationship notes only — no confidential matter details.")

contacts = load_contacts()
companies = load_companies()
cfg = load_config()
goal = cfg.get("daily_goal_count", 4)


def org_name(c):
    return companies.get(c.get("company_id"), "") if c.get("company_id") else ""


def render_contact_card(c):
    ds = days_since(c)
    last = f"{ds}d ago" if ds is not None else "never"
    dot = COLOR_DOT.get(c.get("priority_color"), "")
    with st.expander(f"{dot} **{c['name']}** — {org_name(c)}  ·  "
                     f"{c.get('cadence_tier','?')} · last: {last}"):
        st.caption(f"Type: {c.get('contact_type') or '—'} · "
                   f"Priority: {c.get('manual_priority') or '—'}/5 · "
                   f"Pref: {c.get('comm_preference') or '—'}")
        if c.get("personal_notes"):
            st.write(c["personal_notes"])
        with st.form(f"log_{c['id']}"):
            cols = st.columns(4)
            ttype = cols[0].selectbox("Touch type", TOUCH_TYPES, key=f"tt_{c['id']}")
            channel = cols[1].selectbox("Channel", CHANNELS, key=f"ch_{c['id']}")
            minutes = cols[2].number_input("Minutes", 0, 600, 10, step=5, key=f"mn_{c['id']}")
            cols[3].caption("Logging resets the cadence clock.")
            notes = st.text_input("Notes (optional)", key=f"nt_{c['id']}")
            if st.form_submit_button("✅ Log it"):
                do_log(c["id"], ttype, channel, minutes, notes, suggested_type="cadence-due")
                st.rerun()


# ============================== TODAY ======================================
if page == "Today":
    st.header("Today")
    done = todays_count()
    pct = min(done / goal, 1.0) if goal else 0.0
    c1, c2 = st.columns([3, 1])
    with c1:
        st.progress(pct, text=f"Daily goal: {done} of {goal} touches logged")
    with c2:
        st.metric("Logged today", done)

    queue = sorted([c for c in contacts if is_overdue(c)],
                   key=lambda c: (-(c.get("manual_priority") or 0), -overdue_by(c)))

    # Cap the daily ask to the goal, shrinking as touches get logged today.
    remaining = max(goal - done, 0)
    todays = queue[:remaining]

    if not queue:
        st.success("Nobody is overdue — you're current with your whole book.")
    elif remaining == 0:
        st.success(f"🎉 You hit today's goal of {goal}. Anything more today is a bonus.")
    else:
        st.subheader(f"Reach out today — your top {len(todays)}")
        st.caption(f"{len(queue)} contacts are due overall; showing only your highest-priority "
                   f"{len(todays)} so it stays manageable. Each one you log frees a slot for the next.")
        for c in todays:
            render_contact_card(c)

    extra = queue[len(todays):]
    if extra:
        with st.expander(f"Show {len(extra)} more due (optional — only if you want to keep going)"):
            for c in extra[:25]:
                render_contact_card(c)

    st.divider()
    st.subheader("Log other BD")
    st.caption("Did something not on the list (a call, an event, a hallway chat)? Log it here.")
    with st.form("log_adhoc"):
        names = {f"{c['name']} — {org_name(c)}": c["id"] for c in contacts}
        cols = st.columns(4)
        who = cols[0].selectbox("Contact", list(names.keys()))
        ttype = cols[1].selectbox("Touch type", TOUCH_TYPES)
        channel = cols[2].selectbox("Channel", CHANNELS)
        minutes = cols[3].number_input("Minutes", 0, 600, 15, step=5)
        notes = st.text_input("Notes (optional)")
        if st.form_submit_button("➕ Log other BD"):
            do_log(names[who], ttype, channel, minutes, notes)
            st.rerun()


# ============================== CONTACTS ===================================
elif page == "Contacts":
    st.header("Contacts")
    f1, f2, f3, f4 = st.columns([2, 1, 1, 1])
    search = f1.text_input("Search name / organization").lower().strip()
    colors = f2.multiselect("Priority", ["Green", "Blue", "Purple"])
    types = f3.multiselect("Type", TYPE_OPTS)
    cads = f4.multiselect("Cadence", CADENCE_OPTS)

    def keep(c):
        if search and search not in (c.get("name", "").lower() + " " + org_name(c).lower()):
            return False
        if colors and c.get("priority_color") not in colors:
            return False
        if types and c.get("contact_type") not in types:
            return False
        if cads and c.get("cadence_tier") not in cads:
            return False
        return True

    filtered = [c for c in contacts if keep(c)]
    st.caption(f"{len(filtered)} of {len(contacts)} contacts")

    table = pd.DataFrame([{
        "": COLOR_DOT.get(c.get("priority_color"), ""),
        "Name": c["name"],
        "Organization": org_name(c),
        "Type": c.get("contact_type") or "",
        "Priority": c.get("manual_priority") or "",
        "Cadence": c.get("cadence_tier") or "",
        "Last contact": (days_since(c) is not None) and f"{days_since(c)}d ago" or "never",
        "Overdue": "⚠️" if is_overdue(c) else "",
    } for c in filtered])
    st.dataframe(table, width="stretch", hide_index=True)

    st.divider()
    st.subheader("Edit a contact")
    pick = {f"{c['name']} — {org_name(c)}": c for c in filtered}
    if pick:
        sel = st.selectbox("Choose", list(pick.keys()))
        c = pick[sel]
        with st.form("edit_contact"):
            a, b = st.columns(2)
            name = a.text_input("Name", c.get("name") or "")
            title = b.text_input("Title", c.get("title") or "")
            email = a.text_input("Email", c.get("email") or "")
            phone = b.text_input("Phone", c.get("phone") or "")
            location = a.text_input("City / location", c.get("location") or "")
            linkedin = b.text_input("LinkedIn URL", c.get("linkedin_url") or "")
            ctype = a.selectbox("Type", TYPE_OPTS,
                                index=TYPE_OPTS.index(c["contact_type"]) if c.get("contact_type") in TYPE_OPTS else 0)
            comm = b.selectbox("Comm preference", COMM_OPTS,
                               index=COMM_OPTS.index(c["comm_preference"]) if c.get("comm_preference") in COMM_OPTS else len(COMM_OPTS) - 1)
            cadence = a.selectbox("Cadence", CADENCE_OPTS,
                                  index=CADENCE_OPTS.index(c["cadence_tier"]) if c.get("cadence_tier") in CADENCE_OPTS else 1)
            seniority = b.selectbox("Seniority / decision power", SENIORITY_OPTS,
                                    index=SENIORITY_OPTS.index(c["seniority"]) if c.get("seniority") in SENIORITY_OPTS else len(SENIORITY_OPTS) - 1)
            priority = st.slider("Manual priority", 1, 5, int(c.get("manual_priority") or 3),
                                 help="Your ranking. The opportunity score (coming soon) sits beside this, never overwrites it.")
            notes = st.text_area("Information learned / relationship notes (public info only)",
                                 c.get("personal_notes") or "", height=120)
            if st.form_submit_button("💾 Save"):
                db.update_contact(c["id"], {
                    "name": name, "title": title or None, "email": email or None,
                    "phone": phone or None, "location": location or None,
                    "linkedin_url": linkedin or None, "contact_type": ctype,
                    "comm_preference": comm, "cadence_tier": cadence,
                    "seniority": seniority, "manual_priority": priority,
                    "personal_notes": notes or None,
                })
                refresh()
                st.success(f"Saved {name}.")
                st.rerun()


# ============================== ADD CONTACT ================================
elif page == "Add contact":
    st.header("Add a contact")
    company_choices = db.get_company_choices()
    comp_names = [c["name"] for c in company_choices]
    with st.form("add_contact"):
        a, b = st.columns(2)
        name = a.text_input("Name *")
        title = b.text_input("Title")
        existing_co = a.selectbox("Organization (existing)", ["—"] + comp_names)
        new_co = b.text_input("…or new organization")
        email = a.text_input("Email")
        phone = b.text_input("Phone")
        location = a.text_input("City / location")
        ctype = b.selectbox("Type", TYPE_OPTS, index=2)
        color = a.selectbox("Priority color", ["Green", "Blue", "Purple"], index=1)
        cadence = b.selectbox("Cadence", CADENCE_OPTS, index=CADENCE_OPTS.index("bimonthly"))
        priority = st.slider("Manual priority", 1, 5, 3)
        notes = st.text_area("Relationship notes (public info only)")
        if st.form_submit_button("➕ Add contact"):
            if not name.strip():
                st.error("Name is required.")
            else:
                company_id = None
                if new_co.strip():
                    company_id = db.ensure_company(new_co)
                elif existing_co != "—":
                    company_id = next(c["id"] for c in company_choices if c["name"] == existing_co)
                db.insert_contact({
                    "name": name.strip(), "title": title or None, "company_id": company_id,
                    "email": email or None, "phone": phone or None, "location": location or None,
                    "contact_type": ctype, "priority_color": color, "cadence_tier": cadence,
                    "manual_priority": priority, "personal_notes": notes or None,
                })
                refresh()
                st.success(f"Added {name}.")
