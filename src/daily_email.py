"""Daily BD-plan email for Project Activator.

Composes a morning email: who to contact today (your 3-5, ranked by opportunity score
with cadence-overdue folded in), why, and the news/signals driving each — plus a short
"what's happening in your network" digest. Sends via Gmail SMTP.

Run: python3 src/daily_email.py [--dry]
Env: GMAIL_SENDER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL (all in .env / CI secrets).
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

DRY = "--dry" in sys.argv
TODAY = dt.date.today()
CADENCE_DAYS = {"weekly": 7, "monthly": 30, "bimonthly": 60,
                "quarterly": 90, "biannual": 180, "annual": 365, "dormant": None}


def parse_date(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


def is_overdue(c):
    interval = CADENCE_DAYS.get(c.get("cadence_tier"))
    if interval is None:
        return False
    last = parse_date(c.get("last_contacted_at"))
    return last is None or (TODAY - last).days >= interval


def opp(c):
    return c.get("opportunity_score") or 0


def build():
    cfg = db.get_config()
    goal = cfg.get("daily_goal_count", 4)
    contacts = db.get_contacts()
    companies = db.get_companies_map()
    signals = db.get_signals(limit=400) if hasattr(db, "get_signals") else []
    sig_by_co = {}
    for s in signals:
        sig_by_co.setdefault(s.get("company_id"), []).append(s)

    def org(c):
        return companies.get(c.get("company_id"), "") if c.get("company_id") else ""

    def sigs(c, n=2):
        s = sorted(sig_by_co.get(c.get("company_id"), []), key=lambda x: -(x.get("score_impact") or 0))
        return s[:n]

    candidates = [c for c in contacts if is_overdue(c) or opp(c) >= 50]
    queue = sorted(candidates, key=lambda c: (-opp(c), -(c.get("manual_priority") or 0)))[:goal]

    # --- HTML ---
    rows = []
    for i, c in enumerate(queue, 1):
        sg = "".join(
            f'<div style="margin:2px 0 2px 12px;font-size:13px;color:#333;">• '
            f'<b>{s.get("type")}</b>: {(s.get("title") or "")[:90]}'
            + (f' &middot; <a href="{s["url"]}">link</a>' if s.get("url") else "") + '</div>'
            for s in sigs(c))
        why = c.get("opportunity_rationale") or ""
        rows.append(
            f'<div style="margin:14px 0;padding:10px;border-left:3px solid #2b6cb0;background:#f7fafc;">'
            f'<div style="font-size:15px;"><b>{i}. {c["name"]}</b> '
            f'<span style="color:#666;">— {org(c)}</span> '
            f'<span style="float:right;color:#2b6cb0;">⭐ {c.get("opportunity_score") if c.get("opportunity_score") is not None else "—"}/100</span></div>'
            f'<div style="font-size:13px;color:#444;margin:4px 0;">{why}</div>'
            f'<div style="font-size:13px;color:#555;">Suggested: a {("note" if c.get("comm_preference")=="email" else c.get("comm_preference") or "touch")} '
            f'&middot; pref: {c.get("comm_preference") or "—"}</div>'
            f'{sg}</div>')

    top_sigs = sorted(signals, key=lambda x: -(x.get("score_impact") or 0))[:5]
    digest = "".join(
        f'<li style="margin:3px 0;font-size:13px;"><b>{companies.get(s.get("company_id"),"")}</b> '
        f'[{s.get("type")}]: {(s.get("title") or "")[:90]}'
        + (f' &middot; <a href="{s["url"]}">link</a>' if s.get("url") else "") + '</li>'
        for s in top_sigs)

    html = (
        f'<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:640px;">'
        f'<h2 style="color:#2b6cb0;margin-bottom:0;">BD plan — {TODAY:%A, %B %-d}</h2>'
        f'<div style="color:#666;font-size:13px;margin-bottom:8px;">'
        f'Your top {len(queue)} to reach out to today (ranked by opportunity).</div>'
        + ("".join(rows) or "<p>Nobody is due today — you're current.</p>")
        + (f'<h3 style="color:#2b6cb0;margin-top:22px;">What\'s happening in your network</h3>'
           f'<ul style="padding-left:18px;">{digest}</ul>' if digest else "")
        + '<div style="color:#999;font-size:11px;margin-top:18px;">'
          'Public info only. Verify conflicts before any pitch.</div></div>')

    text = f"BD plan — {TODAY:%A, %B %-d}\nTop {len(queue)} to contact today:\n" + "\n".join(
        f"{i}. {c['name']} — {org(c)} (opp {c.get('opportunity_score')}) : "
        f"{c.get('opportunity_rationale') or ''}" for i, c in enumerate(queue, 1))

    subject = f"BD plan — {TODAY:%b %-d}: {len(queue)} to contact"
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
