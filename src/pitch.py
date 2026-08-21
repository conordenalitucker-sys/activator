"""Pitch Reps helpers — logic shared by the dashboard's editable pitch log, the
Google Sheet mirror, and the "email me the spreadsheet" button.

Kept out of app.py so the row-diffing (what actually gets written back to Supabase
when a row in the pitch table is edited) and the exported table can be unit-tested
without Streamlit. Scoring lives in planning.py (pitch_strength).

Python 3.9 compatible.
"""
from __future__ import annotations

import csv
import io
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

WORK_TYPES = ["litigation", "cross-sell"]
OUTCOMES = ["pending", "landed", "lost"]
SOURCE_HINTS = ["existing client", "referral", "firm colleague", "RFP / panel",
                "event", "inbound", "network"]

# editor column -> pitch_reps column, for the plain text fields
TEXT_COLUMNS = (("Partner", "partner"), ("Source", "source"),
                ("Description", "description"), ("Type", "work_type"))


def _isna(v) -> bool:
    return v is None or v != v  # NaN is the only value that isn't equal to itself


def as_iso(v):
    """A date cell (date, pandas Timestamp, string, or blank) as an ISO date string."""
    if _isna(v):
        return None
    if hasattr(v, "date"):
        v = v.date()
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)[:10] or None


def as_text(v):
    if _isna(v):
        return None
    return str(v).strip() or None


def as_num(v):
    if _isna(v):
        return None
    return float(v)


def diff_row(row, orig: dict, id_by_label: dict, today_iso: str) -> dict:
    """Fields that changed between an edited table row and its stored pitch_reps record.

    `row` is a mapping of editor columns (Date/Partner/Potential client/Source/
    Potential value/Description/Type/Outcome). Returns {} when nothing changed, so the
    caller can skip the write. Setting an outcome stamps (or clears) outcome_date.
    """
    fields = {}

    new_date = as_iso(row["Date"])
    if new_date and new_date != (orig.get("date") or "")[:10]:
        fields["date"] = new_date

    for col, key in TEXT_COLUMNS:
        new_val = as_text(row[col])
        if new_val != (orig.get(key) or None):
            fields[key] = new_val

    new_val = as_num(row["Potential value"])
    old_val = (float(orig["potential_value"])
               if orig.get("potential_value") is not None else None)
    if new_val != old_val:
        fields["potential_value"] = new_val

    new_cid = id_by_label.get(as_text(row["Potential client"]) or "")
    if new_cid and new_cid != orig.get("contact_id"):
        fields["contact_id"] = new_cid

    new_outcome = as_text(row["Outcome"]) or "pending"
    if new_outcome != (orig.get("outcome") or "pending"):
        fields["outcome"] = new_outcome
        fields["outcome_date"] = None if new_outcome == "pending" else today_iso

    return fields


# --- the exported table (Google Sheet tab, CSV attachment, email body) ------

EXPORT_HEADERS = ["Date", "Partner", "Potential Client", "Organization", "Source",
                  "Potential Value", "Description", "Type", "Outcome", "Outcome Date", "id"]


def _money(v):
    return f"${float(v):,.0f}" if v not in (None, "") else ""


def export_rows(reps, contacts_by_id, companies_by_id=None):
    """[headers, *rows] for the pitch log — one shape for the sheet, CSV and email."""
    companies_by_id = companies_by_id or {}
    out = [list(EXPORT_HEADERS)]
    for r in (reps or []):
        c = contacts_by_id.get(r.get("contact_id")) or {}
        out.append([
            (r.get("date") or "")[:10],
            r.get("partner") or "",
            c.get("name") or "",
            companies_by_id.get(c.get("company_id"), "") if c.get("company_id") else "",
            r.get("source") or "",
            _money(r.get("potential_value")),
            r.get("description") or "",
            r.get("work_type") or "",
            r.get("outcome") or "pending",
            (r.get("outcome_date") or "")[:10],
            r.get("id") or "",
        ])
    return out


def summary(reps):
    """Headline numbers for the log: counts, win rate, and pipeline/landed value."""
    reps = reps or []
    landed = [r for r in reps if r.get("outcome") == "landed"]
    lost = [r for r in reps if r.get("outcome") == "lost"]
    open_ = open_reps(reps)
    decided = len(landed) + len(lost)

    def total(rows):
        return sum(float(r.get("potential_value") or 0) for r in rows)

    return {"total": len(reps), "landed": len(landed), "lost": len(lost),
            "open": len(open_), "win_rate": (len(landed) / decided) if decided else None,
            "open_value": total(open_), "landed_value": total(landed)}


def open_reps(reps):
    return [r for r in (reps or []) if (r.get("outcome") or "pending") == "pending"]


def to_csv(rows) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue()


def _esc(v):
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def to_html(rows, stats) -> str:
    """A plain, mail-client-safe HTML table (inline styles only)."""
    head, body = rows[0], rows[1:]
    show = [i for i, h in enumerate(head) if h != "id"]
    th = "".join(f'<th style="padding:6px 10px;border-bottom:2px solid #ccc;'
                 f'text-align:left;font-size:13px">{_esc(head[i])}</th>' for i in show)
    trs = []
    for r in body:
        tds = "".join(f'<td style="padding:6px 10px;border-bottom:1px solid #eee;'
                      f'font-size:13px;vertical-align:top">{_esc(r[i])}</td>' for i in show)
        trs.append(f"<tr>{tds}</tr>")
    wr = f"{stats['win_rate'] * 100:.0f}%" if stats["win_rate"] is not None else "—"
    return (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif">'
        "<h2 style='margin:0 0 4px'>Pitch Reps</h2>"
        f"<p style='color:#555;margin:0 0 14px;font-size:14px'>"
        f"{stats['total']} reps · {stats['landed']} landed ({wr} win rate) · "
        f"{stats['open']} open worth {_money(stats['open_value'])} · "
        f"landed {_money(stats['landed_value'])}</p>"
        f"<table style='border-collapse:collapse'><thead><tr>{th}</tr></thead>"
        f"<tbody>{''.join(trs)}</tbody></table>"
        "<p style='color:#888;font-size:12px;margin-top:14px'>"
        "Full spreadsheet attached as CSV. Verify conflicts before pitching.</p></div>")


def to_text(rows, stats) -> str:
    wr = f"{stats['win_rate'] * 100:.0f}%" if stats["win_rate"] is not None else "—"
    lines = [f"Pitch Reps — {stats['total']} reps, {stats['landed']} landed ({wr} win rate), "
             f"{stats['open']} open worth {_money(stats['open_value'])}.", ""]
    for r in rows[1:]:
        lines.append(f"{r[0]}  {r[2] or '—'} ({r[3] or '—'})  w/ {r[1] or '—'}  "
                     f"{r[5] or '—'}  {r[7] or '—'}  [{r[8]}]  {r[6]}")
    lines.append("")
    lines.append("Full spreadsheet attached as CSV.")
    return "\n".join(lines)


def config_status() -> str:
    """What the running app can actually see of the mail settings — so a failed send
    says whether the secrets are missing/misnamed or the send itself broke. Never
    prints the app password, only whether it's there and how long it is."""
    def state(key, secret=False):
        v = os.environ.get(key)
        if not v:
            return f"{key}: MISSING"
        return f"{key}: set, {len(v)} chars" if secret else f"{key}: {v}"
    return " · ".join([state("GMAIL_SENDER"), state("GMAIL_APP_PASSWORD", secret=True),
                       state("RECIPIENT_EMAIL")])


def send_log_email(reps, contacts_by_id, companies_by_id=None, to=None, today_iso=""):
    """Email the current pitch log (HTML table + CSV attachment). Returns the address
    it went to. Needs GMAIL_SENDER / GMAIL_APP_PASSWORD in the environment."""
    sender = (os.environ.get("GMAIL_SENDER") or "").strip()
    # Google shows app passwords in four spaced blocks but expects them unspaced; accept
    # either, and tolerate a stray newline from a pasted secrets file.
    pw = "".join((os.environ.get("GMAIL_APP_PASSWORD") or "").split())
    if not sender or not pw:
        raise RuntimeError("Email isn't configured — GMAIL_SENDER and GMAIL_APP_PASSWORD "
                           "must be set (add them to the app's secrets).")
    to = (to or os.environ.get("RECIPIENT_EMAIL") or sender).strip()
    rows = export_rows(reps, contacts_by_id, companies_by_id)
    stats = summary(reps)

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"Pitch Reps — {stats['total']} reps, {stats['landed']} landed"
    msg["From"] = sender
    msg["To"] = to
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(to_text(rows, stats), "plain"))
    alt.attach(MIMEText(to_html(rows, stats), "html"))
    msg.attach(alt)
    csv_part = MIMEText(to_csv(rows), "csv", "utf-8")  # text/csv opens straight into Excel
    csv_part.add_header("Content-Disposition", "attachment",
                        filename=f"pitch-reps-{today_iso or 'export'}.csv")
    msg.attach(csv_part)

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(sender, pw)
        s.sendmail(sender, [to], msg.as_string())
    return to
