"""Project Activator — dashboard (Streamlit).

Phase 2: Today queue (cadence-driven) + daily progress, Contacts list/filter/edit
with the manual-priority slider, and Add-contact. Opportunity score, monitoring,
and drafting layer on in later phases.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from collections import defaultdict
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
import planning  # noqa: E402  (shared scoring + daily-plan core)

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
TOUCH_TYPES = ["personal", "pitch", "legal-update", "industry-check", "hello", "client-work", "other"]
CHANNELS = ["email", "LinkedIn", "call", "in-person", "event", "text", "other"]
CHANNEL_LABEL = {"text": "text / DM"}


def channel_label(x):
    return CHANNEL_LABEL.get(x, x)

# Use a fixed app timezone so "today" is consistent everywhere — the cloud server
# runs in UTC, which would otherwise roll over to tomorrow in the evening Pacific.
try:
    from zoneinfo import ZoneInfo
    APP_TZ = ZoneInfo("America/Los_Angeles")
    TODAY = dt.datetime.now(APP_TZ).date()
except Exception:
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


@st.cache_data(ttl=30)
def todays_minutes():
    return db.todays_minutes(TODAY_ISO)


@st.cache_data(ttl=60)
def load_interactions():
    return db.all_interactions_brief()


@st.cache_data(ttl=60)
def load_business():
    return db.get_business()


@st.cache_data(ttl=120)
def load_signals():
    try:
        return db.get_signals(limit=400)
    except Exception:
        return []


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


def rescore():
    """Recompute opportunity scores instantly from current data + CACHED firm-fit
    (no Claude) — so same-day edits re-rank without waiting for the nightly run."""
    cos = {c["id"]: c for c in db.get_companies_full()}
    sigs = {}
    for s in load_signals():
        sigs.setdefault(s.get("company_id"), []).append(s)
    biz_ids = {b["contact_id"] for b in db.get_business() if b.get("contact_id")}
    weights = load_config().get("scoring_weights")
    for c in db.get_contacts():
        score, rationale, _comps, _top = planning.compute_opportunity(
            c, cos.get(c.get("company_id")), sigs.get(c.get("company_id"), []),
            biz_ids, weights, TODAY)
        db.update_contact(c["id"], {
            "opportunity_score": score, "opportunity_rationale": rationale,
            "score_updated_at": dt.datetime.utcnow().isoformat()})
    refresh()


# ---------------------------------------------------------------------------
st.sidebar.title("📇 Project Activator")
page = st.sidebar.radio("Go to", ["Today", "Signals", "Contacts", "Companies",
                                  "Add contact", "Business In", "Activity", "Settings"])
st.sidebar.caption("Public info + relationship notes only — no confidential matter details.")

contacts = load_contacts()
companies = load_companies()
cfg = load_config()
goal = cfg.get("daily_goal_count", 4)

sig_by_company = {}
for _s in load_signals():
    sig_by_company.setdefault(_s.get("company_id"), []).append(_s)


def org_name(c):
    return companies.get(c.get("company_id"), "") if c.get("company_id") else ""


def opp(c):
    return c.get("opportunity_score") or 0


def signals_for(c, n=2):
    s = sorted(sig_by_company.get(c.get("company_id"), []),
               key=lambda x: -(x.get("score_impact") or 0))
    return s[:n]


def interest_catalog():
    s = set()
    for c in contacts:
        for i in (c.get("interests") or []):
            s.add(i)
    return sorted(s)


INTEREST_CATALOG = interest_catalog()


def render_contact_card(c):
    ds = days_since(c)
    last = f"{ds}d ago" if ds is not None else "never"
    dot = COLOR_DOT.get(c.get("priority_color"), "")
    with st.expander(f"{dot} **{c['name']}** — {org_name(c)}  ·  "
                     f"{c.get('cadence_tier','?')} · last: {last}"):
        st.caption(f"Type: {c.get('contact_type') or '—'} · "
                   f"Priority: {c.get('manual_priority') or '—'}/5 · "
                   f"⭐ Opportunity: {c.get('opportunity_score') if c.get('opportunity_score') is not None else '—'}/100 · "
                   f"Pref: {c.get('comm_preference') or '—'}")
        if c.get("opportunity_rationale"):
            st.caption(c["opportunity_rationale"])
        for s in signals_for(c):
            st.markdown(f"• **{s.get('type')}** — {(s.get('title') or '')[:80]}"
                        + (f"  [link]({s['url']})" if s.get("url") else ""))
        if c.get("interests"):
            st.caption("Interests: " + ", ".join(c["interests"]))
        if c.get("personal_notes"):
            st.write(c["personal_notes"])
        with st.form(f"log_{c['id']}"):
            cols = st.columns(4)
            ttype = cols[0].selectbox("Touch type", TOUCH_TYPES, key=f"tt_{c['id']}")
            channel = cols[1].selectbox("Channel", CHANNELS, format_func=channel_label, key=f"ch_{c['id']}")
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
    mins = todays_minutes()
    goal_min = cfg.get("daily_goal_minutes") or 0
    st.progress(min(done / goal, 1.0) if goal else 0.0,
                text=f"Touch goal: {done} of {goal} contacts")
    if goal_min:
        st.progress(min(mins / goal_min, 1.0) if goal_min else 0.0,
                    text=f"Time goal: {mins} of {goal_min} min")
    m1, m2 = st.columns(2)
    m1.metric("Logged today", done)
    m2.metric("Minutes today", mins)

    # Score freshness + on-demand recompute.
    scored = [c.get("score_updated_at") for c in contacts if c.get("score_updated_at")]
    last_scored = max(scored) if scored else None
    rc1, rc2 = st.columns([3, 1])
    rc1.caption("Opportunity scores as of "
                + (str(planning.parse_date(last_scored)) if last_scored else "— not yet scored"))
    if rc2.button("🔄 Rescore now"):
        rescore()
        st.rerun()

    # Shared daily plan — identical to the morning email.
    opp_picks, cad_picks = planning.select_daily_plan(contacts, sig_by_company, goal, TODAY)
    st.subheader("🔔 Opportunity-driven")
    if opp_picks:
        for c in opp_picks:
            render_contact_card(c)
    else:
        st.caption("No fresh developments today.")
    st.subheader("🔁 Keeping cadence")
    if cad_picks:
        for c in cad_picks:
            render_contact_card(c)
    else:
        st.caption("Nobody overdue — you're current.")

    plan_ids = {c["id"] for c in opp_picks + cad_picks}
    extra = sorted([c for c in contacts if is_overdue(c) and c["id"] not in plan_ids],
                   key=lambda c: -overdue_by(c))
    if extra:
        with st.expander(f"Show {len(extra)} more overdue (optional)"):
            for c in extra[:25]:
                render_contact_card(c)

    st.divider()
    st.subheader("Log other BD")
    st.caption("Did something not on the list (a call, an event, a hallway chat)? Log it here.")
    NEW_CONTACT = "➕ New contact…"
    with st.form("log_adhoc"):
        names = {f"{c['name']} — {org_name(c)}": c["id"] for c in contacts}
        cols = st.columns(4)
        who = cols[0].selectbox("Contact", [NEW_CONTACT] + list(names.keys()))
        ttype = cols[1].selectbox("Touch type", TOUCH_TYPES)
        channel = cols[2].selectbox("Channel", CHANNELS, format_func=channel_label)
        minutes = cols[3].number_input("Minutes", 0, 600, 15, step=5)
        st.caption(f"Pick **{NEW_CONTACT}** to add someone new — fill these in:")
        nc1, nc2 = st.columns(2)
        new_name = nc1.text_input("New contact name")
        new_org = nc2.text_input("New contact organization (optional)")
        notes = st.text_input("Notes (optional)")
        if st.form_submit_button("➕ Log other BD"):
            if who == NEW_CONTACT:
                if not new_name.strip():
                    st.error("Enter a name for the new contact.")
                    st.stop()
                company_id = db.ensure_company(new_org) if new_org.strip() else None
                created = db.insert_contact({"name": new_name.strip(), "company_id": company_id})
                cid = created[0]["id"]
            else:
                cid = names[who]
            do_log(cid, ttype, channel, minutes, notes)
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
        "Interests": ", ".join(c.get("interests") or []),
        "Priority": c.get("manual_priority") or "",
        "Opp.": c.get("opportunity_score") if c.get("opportunity_score") is not None else "",
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
        edit_cos = db.get_company_choices()
        edit_co_names = [x["name"] for x in edit_cos]
        with st.form("edit_contact"):
            a, b = st.columns(2)
            name = a.text_input("Name", c.get("name") or "")
            title = b.text_input("Title", c.get("title") or "")
            cur_org = org_name(c)
            org_opts = ["— none —"] + edit_co_names
            existing_co = a.selectbox("Organization", org_opts,
                                      index=org_opts.index(cur_org) if cur_org in org_opts else 0,
                                      help="Move the contact to a different organization here.")
            new_co = b.text_input("…or move to a NEW organization")
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
            if c.get("opportunity_score") is not None:
                st.info(f"⭐ Opportunity score: {c.get('opportunity_score')}/100 — "
                        f"{c.get('opportunity_rationale', '')}")
            priority = st.slider("Manual priority", 1, 5, int(c.get("manual_priority") or 3),
                                 help="Your ranking. The opportunity score sits beside it, never overwrites it.")
            cur_int = c.get("interests") or []
            int_opts = sorted(set(INTEREST_CATALOG) | set(cur_int))
            interests_sel = st.multiselect(
                "Interests (topics they care about)", int_opts, default=cur_int,
                help="e.g. first amendment, casino regulation, sports betting. "
                     "These will feed the opportunity score and which news raises this contact.")
            new_interests = st.text_input("Add new interests (comma-separated)")
            notes = st.text_area("Information learned / relationship notes (public info only)",
                                 c.get("personal_notes") or "", height=120)
            if st.form_submit_button("💾 Save"):
                merged_int = sorted(set(interests_sel) |
                                    {x.strip() for x in new_interests.split(",") if x.strip()})
                if new_co.strip():
                    company_id = db.ensure_company(new_co)
                elif existing_co != "— none —":
                    company_id = next(x["id"] for x in edit_cos if x["name"] == existing_co)
                else:
                    company_id = None
                db.update_contact(c["id"], {
                    "name": name, "title": title or None, "company_id": company_id,
                    "email": email or None,
                    "phone": phone or None, "location": location or None,
                    "linkedin_url": linkedin or None, "contact_type": ctype,
                    "comm_preference": comm, "cadence_tier": cadence,
                    "seniority": seniority, "manual_priority": priority,
                    "interests": merged_int,
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
        add_interests = st.text_input("Interests (comma-separated)",
                                      help="e.g. first amendment, casino regulation, sports betting")
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
                    "manual_priority": priority,
                    "interests": [x.strip() for x in add_interests.split(",") if x.strip()],
                    "personal_notes": notes or None,
                })
                refresh()
                st.success(f"Added {name}.")


# ============================== BUSINESS IN ================================
elif page == "Business In":
    st.header("Business In")
    st.caption("Track business that came IN through a connection (referral / origination). "
               "Keep descriptions general — no confidential matter details.")
    try:
        biz = load_business()
    except Exception:
        st.warning("Run migration 002 in the Supabase SQL Editor to enable this page "
                   "(it adds the business-origination table).")
        st.stop()
    name_by_id = {c["id"]: c["name"] for c in contacts}

    with st.form("add_business"):
        names = {f"{c['name']} — {org_name(c)}": c["id"] for c in contacts}
        cols = st.columns([2, 1, 1])
        who = cols[0].selectbox("Source connection", list(names.keys()))
        when = cols[1].date_input("Date", TODAY)
        value = cols[2].number_input("Est. value ($, optional)", 0, step=1000)
        desc = st.text_input("Description (general)")
        if st.form_submit_button("➕ Record business in"):
            db.insert_business({
                "contact_id": names[who],
                "date": when.isoformat(),
                "description": desc or None,
                "est_value": value or None,
            })
            refresh()
            st.success("Recorded.")
            st.rerun()

    if biz:
        total = sum((b.get("est_value") or 0) for b in biz)
        c1, c2 = st.columns(2)
        c1.metric("Originations recorded", len(biz))
        c2.metric("Est. value tracked", f"${total:,.0f}")
        st.dataframe(pd.DataFrame([{
            "Date": (b.get("date") or "")[:10],
            "Source": name_by_id.get(b.get("contact_id"), "—"),
            "Description": b.get("description") or "",
            "Est. value": f"${(b.get('est_value') or 0):,.0f}" if b.get("est_value") else "",
        } for b in biz]), width="stretch", hide_index=True)

        agg = defaultdict(lambda: [0, 0.0])
        for b in biz:
            a = agg[b.get("contact_id")]
            a[0] += 1
            a[1] += (b.get("est_value") or 0)
        st.subheader("By source connection")
        st.dataframe(pd.DataFrame([{
            "Source": name_by_id.get(k, "—"), "Count": v[0], "Est. value": f"${v[1]:,.0f}",
        } for k, v in sorted(agg.items(), key=lambda kv: -kv[1][1])]),
            width="stretch", hide_index=True)
    else:
        st.info("No business-in recorded yet.")


# ============================== ACTIVITY ==================================
elif page == "Activity":
    st.header("Activity over time")
    ints = load_interactions()

    def window(days):
        cutoff = (TODAY - dt.timedelta(days=days)).isoformat()
        rows = [r for r in ints if (r.get("date") or "") >= cutoff]
        return len(rows), sum((r.get("duration_minutes") or 0) for r in rows)

    w7 = window(7)
    w30 = window(30)
    all_min = sum((r.get("duration_minutes") or 0) for r in ints)
    c1, c2, c3 = st.columns(3)
    c1.metric("Last 7 days", f"{w7[0]} touches", f"{w7[1]} min", delta_color="off")
    c2.metric("Last 30 days", f"{w30[0]} touches", f"{w30[1]} min", delta_color="off")
    c3.metric("All time", f"{len(ints)} touches", f"{all_min} min", delta_color="off")

    per_day = defaultdict(lambda: [0, 0])
    for r in ints:
        d = (r.get("date") or "")[:10]
        per_day[d][0] += 1
        per_day[d][1] += (r.get("duration_minutes") or 0)
    days = [(TODAY - dt.timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    st.subheader("Daily (last 14 days)")
    st.bar_chart(pd.DataFrame({
        "date": days,
        "touches": [per_day[d][0] for d in days],
        "minutes": [per_day[d][1] for d in days],
    }).set_index("date"))

    by_type = defaultdict(int)
    for r in ints:
        by_type[r.get("type") or "—"] += 1
    st.subheader("By touch type (all time)")
    st.dataframe(pd.DataFrame([{"Touch type": k, "Count": v}
                               for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])]),
                 width="stretch", hide_index=True)


# ============================== SIGNALS ===================================
elif page == "Signals":
    st.header("Signals")
    st.caption("Public trigger events around your contacts' companies, corporate families, "
               "industries, and interests. Found by the monitoring job; Claude-filtered for relevance.")
    try:
        sigs = db.get_signals(limit=300)
    except Exception:
        st.info("No signals yet — run the monitoring job.")
        st.stop()
    co_name = {c["id"]: c["name"] for c in db.get_companies_full()}
    if not sigs:
        st.info("No signals yet. The monitoring job will populate these.")
    else:
        types = sorted({s.get("type") for s in sigs if s.get("type")})
        f1, f2 = st.columns(2)
        tfilter = f1.multiselect("Type", types)
        cfilter = f2.multiselect("Company", sorted({co_name.get(s["company_id"], "?") for s in sigs}))
        shown = [s for s in sigs
                 if (not tfilter or s.get("type") in tfilter)
                 and (not cfilter or co_name.get(s["company_id"]) in cfilter)]
        st.caption(f"{len(shown)} signals (highest BD relevance first)")
        for s in sorted(shown, key=lambda s: -(s.get("score_impact") or 0)):
            head = (f"[{s.get('type')}] {co_name.get(s['company_id'], '?')} · "
                    f"score {s.get('score_impact')} · {s.get('event_date') or ''} — "
                    f"{(s.get('title') or '')[:80]}")
            with st.expander(head):
                st.write(s.get("summary") or "")
                if s.get("url"):
                    st.markdown(f"[source]({s['url']}) · {s.get('source', '')}")
                if st.button("Dismiss", key=f"dis_{s['id']}"):
                    db.dismiss_signal(s["id"])
                    refresh()
                    st.rerun()


# ============================== COMPANIES =================================
elif page == "Companies":
    st.header("Companies & monitoring")
    cos = db.get_companies_full()
    if not cos:
        st.info("No companies yet.")
    else:
        names = {c["name"]: c for c in cos}
        co = names[st.selectbox("Company", list(names.keys()))]

        st.subheader("Monitoring settings")
        with st.form("company_mon"):
            sector = st.text_input("Sector / industry", co.get("sector") or "")
            segment = st.text_input("Segment focus (narrow big parents, e.g. 'Amazon Studios')",
                                    co.get("segment_focus") or "",
                                    help="For large companies, the contact's specific unit. "
                                         "Monitoring uses this instead of the broad parent name.")
            juris = st.text_input("Jurisdiction focus (e.g. 'California')",
                                  co.get("jurisdiction_focus") or "",
                                  help="Narrows court + news monitoring to this state/region.")
            home_state = st.text_input("Home state (e.g. CA — used for state regulators)",
                                       co.get("home_state") or "")
            industries = st.text_input("Industries to monitor (comma-separated)",
                                       ", ".join(co.get("industries") or []))
            watch = st.text_input("Custom watch terms (comma-separated)",
                                  ", ".join(co.get("watch_terms") or []))
            track_reg = st.checkbox("Track this company's STATE regulators",
                                    value=bool(co.get("track_state_regulators")),
                                    help="Turn on to monitor the home-state regulator "
                                         "(e.g. gaming/insurance/utility commissions).")
            if st.form_submit_button("💾 Save monitoring settings"):
                try:
                    db.update_company(co["id"], {
                        "sector": sector or None, "home_state": home_state or None,
                        "segment_focus": segment or None, "jurisdiction_focus": juris or None,
                        "industries": [x.strip() for x in industries.split(",") if x.strip()],
                        "watch_terms": [x.strip() for x in watch.split(",") if x.strip()],
                        "track_state_regulators": track_reg,
                    })
                    refresh()
                    st.success("Saved.")
                    st.rerun()
                except Exception:
                    st.warning("Run migration 003 in the SQL Editor to enable these fields.")

        st.subheader("Corporate family & entities to monitor")
        st.caption("Add parents, subsidiaries, affiliates, peers, or industry entities — "
                   "including private ones (e.g. Flynt Management Group → Hustler, Hustler Casino).")
        ents = db.get_entities(co["id"])
        if ents:
            st.dataframe(pd.DataFrame([{
                "Name": e["name"], "Relationship": e.get("type"),
                "Monitored": "✓" if e.get("enabled", True) else "—",
            } for e in ents]), width="stretch", hide_index=True)
        with st.form("add_entity"):
            cc = st.columns([2, 1, 1])
            en = cc[0].text_input("Entity name")
            etype = cc[1].selectbox("Relationship", ["parent", "subsidiary", "affiliate",
                                    "peer-competitor", "customer-supplier", "co-defendant", "industry"])
            emon = cc[2].checkbox("Monitor", value=True)
            if st.form_submit_button("➕ Add entity"):
                if en.strip():
                    prox = {"parent": 1.0, "subsidiary": 1.0, "affiliate": 0.8,
                            "peer-competitor": 0.6, "customer-supplier": 0.6,
                            "co-defendant": 0.7, "industry": 0.4}.get(etype, 0.8)
                    db.insert_entity({"name": en.strip(), "type": etype,
                                      "related_company_id": co["id"], "enabled": emon,
                                      "source": "manual", "proximity_weight": prox})
                    refresh()
                    st.success(f"Added {en}.")
                    st.rerun()
        if ents:
            rm = st.selectbox("Remove an entity", ["—"] + [e["name"] for e in ents])
            if st.button("Remove selected") and rm != "—":
                db.hard_delete_entity(next(e["id"] for e in ents if e["name"] == rm))
                refresh()
                st.rerun()


# ============================== SETTINGS ===================================
elif page == "Settings":
    st.header("Settings")
    st.caption(f"Daily touch goal: **{goal}** · time goal: "
               f"**{cfg.get('daily_goal_minutes') or 'none'}**")
    with st.form("settings"):
        new_goal = st.number_input("Daily touch goal (contacts)", min_value=1, max_value=20,
                                   value=int(goal), step=1,
                                   help="How many contacts you aim to reach out to each day.")
        new_min = st.number_input("Daily time goal (minutes, 0 = none)", min_value=0, max_value=600,
                                  value=int(cfg.get("daily_goal_minutes") or 0), step=5,
                                  help="Optional: minutes of BD per day to aim for.")
        if st.form_submit_button("💾 Save"):
            db.update_config({"daily_goal_count": int(new_goal),
                              "daily_goal_minutes": int(new_min) or None})
            refresh()
            st.success("Settings saved.")
