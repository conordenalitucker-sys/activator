"""Daily BD-plan email for Project Activator.

Two sections so news never crowds out relationship-keeping:
  - Opportunity-driven : contacts with live signals (news/commentary/litigation),
                         ranked by opportunity score.
  - Keeping cadence    : overdue-by-cadence contacts (always at least MIN_CADENCE),
                         there purely to stay in touch.
Plus a "Record BD" button that opens the dashboard (handy on the phone).

Run: python3 src/daily_email.py [--dry]
Env: GMAIL_SENDER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL, optional ACTIVATOR_APP_URL.
Python 3.9 compatible.
"""
from __future__ import annotations

import datetime as dt
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402  (loads .env)
import anthropic  # noqa: E402

DRY = "--dry" in sys.argv
TODAY = dt.date.today()
MIN_CADENCE = 2  # always include at least this many pure-cadence contacts
MODEL = "claude-haiku-4-5-20251001"
APP_URL = os.environ.get("ACTIVATOR_APP_URL", "https://m6frwjbqtmj52nscwqg3lt.streamlit.app")
CADENCE_DAYS = {"weekly": 7, "monthly": 30, "bimonthly": 60,
                "quarterly": 90, "biannual": 180, "annual": 365, "dormant": None}


def parse_date(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


def overdue_days(c):
    interval = CADENCE_DAYS.get(c.get("cadence_tier"))
    if interval is None:
        return -10 ** 9
    last = parse_date(c.get("last_contacted_at"))
    return ((TODAY - last).days - interval) if last else 10 ** 9


def is_overdue(c):
    return CADENCE_DAYS.get(c.get("cadence_tier")) is not None and overdue_days(c) >= 0


def opp(c):
    return c.get("opportunity_score") or 0


def suggest_message(client, c, org_name, top_sig):
    """Generate a SHORT, ready-to-edit outreach message for ONE picked contact.

    Returns (archetype, text) or None on any failure (so the email never crashes).
    Public info only — never reference confidential/representation details. Picks an
    archetype from the contact's comm_preference and whether a driving signal exists:
      - signal + email/unknown pref  -> EMAIL forwarding a relevant story
      - text / LinkedIn pref         -> short TEXT / DM note
      - otherwise (cadence only)     -> warm RECONNECT check-in
    """
    pref = (c.get("comm_preference") or "unknown").strip().lower()
    sig_title = (top_sig.get("title") or "").strip() if top_sig else ""
    if sig_title and pref in ("email", "unknown", ""):
        archetype = "email"
        ask = ("Draft a SHORT email (1-3 sentences) forwarding a relevant news story to this "
               "contact. Open warmly (e.g. \"thought you'd find this relevant\"), reference the "
               "signal/story by topic, and note the user will paste the link. Do NOT invent the "
               "link or quote the article.")
    elif pref in ("text", "linkedin"):
        archetype = "text"
        ask = ("Draft a SHORT, warm, casual text/DM note (1-2 sentences) to reconnect or check in. "
               "Keep it conversational, not salesy.")
    else:
        archetype = "reconnect"
        ask = ("Draft a SHORT, warm reconnect note (1-3 sentences) checking in. You may gently "
               "reference how long it's been since they last spoke.")
    last = parse_date(c.get("last_contacted_at"))
    last_txt = f"{(TODAY - last).days} days ago" if last else "no record / never"
    interests = ", ".join(c.get("interests") or []) or "unknown"
    prompt = (
        "You are helping a Steptoe LLP attorney write a quick, personal business-development "
        "outreach. Use ONLY the public facts provided below — never mention any client matter, "
        "representation, confidential detail, or anything not given. Do not fabricate facts about "
        "the person. Professional but warm; ready for the attorney to lightly edit.\n\n"
        f"Contact: {c.get('name')}\n"
        f"Organization: {org_name or 'unknown'}\n"
        f"Communication preference: {pref}\n"
        f"Their interests: {interests}\n"
        f"Personal notes (public): {c.get('personal_notes') or 'none'}\n"
        f"Last contacted: {last_txt}\n"
        f"Relevant signal/story title: {sig_title or 'none'}\n\n"
        f"{ask}\n"
        "Return ONLY the message text itself — no greeting label, no subject line, no quotes, "
        "no commentary."
    )
    try:
        msg = client.messages.create(model=MODEL, max_tokens=200,
                                     messages=[{"role": "user", "content": prompt}])
        text = msg.content[0].text.strip().strip('"').strip()
        if not text:
            return None
        return archetype, text
    except Exception:
        return None


def build():
    cfg = db.get_config()
    goal = cfg.get("daily_goal_count", 4)
    client = None
    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    except Exception:
        client = None
    contacts = db.get_contacts()
    companies = db.get_companies_map()
    co_full = {c["id"]: c for c in db.get_companies_full()}
    signals = db.get_signals(limit=400)
    sig_by_co = {}
    for s in signals:
        sig_by_co.setdefault(s.get("company_id"), []).append(s)

    # Roster maps for cross-sell colleague suggestions.
    practices = {p["name"].lower(): p["id"] for p in db.get("practice_areas?select=id,name")}
    atts = {a["id"]: a for a in db.get("attorneys?select=id,name,status")}
    prac_to_atts = {}
    for ln in db.get("attorney_practices?select=attorney_id,practice_area_id"):
        prac_to_atts.setdefault(ln["practice_area_id"], []).append(ln["attorney_id"])

    def colleagues(practice_name, n=4):
        pid = practices.get((practice_name or "").lower())
        if not pid:
            return []
        names = [atts[a]["name"] for a in prac_to_atts.get(pid, [])
                 if atts.get(a) and atts[a].get("status") == "active"]
        return names[:n]

    def org(c):
        return companies.get(c.get("company_id"), "") if c.get("company_id") else ""

    def sigs(c, n=2):
        s = sorted(sig_by_co.get(c.get("company_id"), []), key=lambda x: -(x.get("score_impact") or 0))
        return s[:n]

    def has_sig(c):
        return len(sig_by_co.get(c.get("company_id"), [])) > 0

    # Opportunity-driven: contacts with live signals, ranked by opportunity.
    opp_pool = sorted([c for c in contacts if has_sig(c)],
                      key=lambda c: (-opp(c), -(c.get("manual_priority") or 0)))
    max_opp = max(goal - MIN_CADENCE, 0)
    opp_picks = opp_pool[:max_opp]
    picked = {c["id"] for c in opp_picks}

    # Keeping cadence: overdue & not already picked. PREFER contacts with NO live signal
    # (there purely to stay in touch), so news can't crowd out relationship-keeping.
    overdue_rest = [c for c in contacts if is_overdue(c) and c["id"] not in picked]
    by_overdue = lambda c: (-overdue_days(c), -(c.get("manual_priority") or 0))
    pure = sorted([c for c in overdue_rest if not has_sig(c)], key=by_overdue)
    withsig = sorted([c for c in overdue_rest if has_sig(c)], key=by_overdue)
    cad_pool = pure + withsig
    cad_picks = cad_pool[:max(MIN_CADENCE, goal - len(opp_picks))]

    # Suggested outreach message per picked contact (one Claude call each, cached here).
    suggestions = {}
    if client is not None:
        for c in list(opp_picks) + list(cad_picks):
            if c["id"] in suggestions:
                continue
            top = sigs(c, 1)
            suggestions[c["id"]] = suggest_message(client, c, org(c), top[0] if top else None)

    # --- HTML helpers ---
    def card(c, show_signals):
        sg = ""
        if show_signals:
            sg = "".join(
                f'<div style="margin:2px 0 2px 12px;font-size:13px;color:#333;">• '
                f'<b>{s.get("type")}</b>: {(s.get("title") or "")[:90]}'
                + (f' &middot; <a href="{s["url"]}">link</a>' if s.get("url") else "") + '</div>'
                for s in sigs(c))
        sug = suggestions.get(c["id"])
        sug_html = ""
        if sug:
            kind, body = sug
            sug_html = (
                f'<div style="margin:6px 0 2px;padding:7px 9px;background:#fffbea;'
                f'border:1px solid #f0e6b8;border-radius:4px;font-size:13px;color:#555;'
                f'font-style:italic;"><span style="font-style:normal;font-weight:bold;'
                f'color:#8a6d00;">Suggested ({kind}):</span> &ldquo;{body}&rdquo;</div>')
        last = parse_date(c.get("last_contacted_at"))
        last_txt = f"{(TODAY - last).days}d ago" if last else "never"
        cross = co_full.get(c.get("company_id"), {}).get("cross_sell_practice")
        coll = colleagues(cross) if (show_signals and cross) else []
        cross_html = (
            f'<div style="margin:4px 0;padding:6px 8px;background:#ebf8ff;border-radius:4px;'
            f'font-size:12px;color:#2c5282;">Outside your litigation/appeals focus — '
            f'<b>{cross}</b>. Colleagues to involve: {", ".join(coll)}.</div>') if coll else ""
        return (
            f'<div style="margin:12px 0;padding:10px;border-left:3px solid #2b6cb0;background:#f7fafc;">'
            f'<div style="font-size:15px;"><b>{c["name"]}</b> '
            f'<span style="color:#666;">— {org(c)}</span>'
            f'<span style="float:right;color:#2b6cb0;">⭐ {c.get("opportunity_score") if c.get("opportunity_score") is not None else "—"}/100</span></div>'
            f'<div style="font-size:13px;color:#444;margin:4px 0;">{c.get("opportunity_rationale") or ""}</div>'
            f'<div style="font-size:12px;color:#777;">last contact: {last_txt} &middot; '
            f'cadence: {c.get("cadence_tier") or "—"} &middot; pref: {c.get("comm_preference") or "—"}</div>'
            f'{cross_html}{sg}{sug_html}</div>')

    button = (
        f'<div style="margin:14px 0;"><a href="{APP_URL}" '
        f'style="display:inline-block;background:#2b6cb0;color:#fff;text-decoration:none;'
        f'padding:11px 20px;border-radius:6px;font-size:15px;font-weight:bold;">📲 Record BD</a>'
        f'<div style="font-size:11px;color:#999;margin-top:4px;">Opens the dashboard to log who you reached out to.</div></div>')

    opp_html = ("".join(card(c, True) for c in opp_picks)
                if opp_picks else '<p style="color:#666;font-size:13px;">No fresh developments today.</p>')
    cad_html = ("".join(card(c, False) for c in cad_picks)
                if cad_picks else '<p style="color:#666;font-size:13px;">Nobody overdue.</p>')

    top_sigs = sorted(signals, key=lambda x: -(x.get("score_impact") or 0))[:5]
    digest = "".join(
        f'<li style="margin:3px 0;font-size:13px;"><b>{companies.get(s.get("company_id"),"")}</b> '
        f'[{s.get("type")}]: {(s.get("title") or "")[:90]}'
        + (f' &middot; <a href="{s["url"]}">link</a>' if s.get("url") else "") + '</li>'
        for s in top_sigs)

    html = (
        f'<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:640px;">'
        f'<h2 style="color:#2b6cb0;margin-bottom:2px;">BD plan — {TODAY:%A, %B %-d}</h2>'
        + button
        + f'<h3 style="color:#2b6cb0;margin:18px 0 2px;">🔔 Opportunity-driven '
          f'<span style="font-weight:normal;font-size:13px;color:#777;">(news &amp; developments)</span></h3>'
        + opp_html
        + f'<h3 style="color:#2b6cb0;margin:18px 0 2px;">🔁 Keeping cadence '
          f'<span style="font-weight:normal;font-size:13px;color:#777;">(stay in touch)</span></h3>'
        + cad_html
        + (f'<h3 style="color:#2b6cb0;margin-top:22px;">What\'s happening in your network</h3>'
           f'<ul style="padding-left:18px;">{digest}</ul>' if digest else "")
        + button
        + '<div style="color:#999;font-size:11px;margin-top:18px;">'
          'Public info only. Verify conflicts before any pitch.</div></div>')

    def line(c):
        base = f"  - {c['name']} ({org(c)}) opp {c.get('opportunity_score')}: {c.get('opportunity_rationale') or ''}"
        sug = suggestions.get(c["id"])
        if sug:
            kind, body = sug
            base += f'\n      Suggested ({kind}): "{body}"'
        return base
    text = (f"BD plan — {TODAY:%A, %B %-d}\nRecord BD: {APP_URL}\n\n"
            f"OPPORTUNITY-DRIVEN (news & developments):\n"
            + ("\n".join(line(c) for c in opp_picks) or "  (none)")
            + "\n\nKEEPING CADENCE (stay in touch):\n"
            + ("\n".join(line(c) for c in cad_picks) or "  (none)"))

    subject = f"BD plan — {TODAY:%b %-d}: {len(opp_picks)} opportunity, {len(cad_picks)} cadence"
    return subject, text, html


def send(subject, text, html):
    sender = os.environ["GMAIL_SENDER"]
    pw = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("RECIPIENT_EMAIL", sender)
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(sender, pw)
        s.sendmail(sender, [to], msg.as_string())
    print(f"Sent BD plan to {to}.")


def main():
    subject, text, html = build()
    if DRY:
        print("SUBJECT:", subject)
        print(text)
        print(f"\n[dry-run — not sent; HTML {len(html)} chars]")
    else:
        send(subject, text, html)


if __name__ == "__main__":
    main()
