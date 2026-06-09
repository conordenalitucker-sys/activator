"""Opportunity-score engine for Project Activator (Phase 4).

Computes a 0-100 opportunity score per contact that sits BESIDE the user's manual
priority (never overwrites it). Blends:
  - firm fit         : does Steptoe plausibly sell what this company needs (Claude, cached on company)
  - trigger strength : recent monitoring signals on the company/family (recency + proximity weighted),
                       with a bonus when a signal matches one of the contact's interests
  - relationship     : manual priority + how overdue the contact is by cadence
  - business-in      : has this contact ever sent business our way

Writes contacts.opportunity_score + opportunity_rationale and a score_snapshots row.
Run: python3 src/score.py
Python 3.9 compatible.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402  (loads .env)
import anthropic  # noqa: E402

MODEL = "claude-haiku-4-5-20251001"
TODAY = dt.date.today()
SIGNAL_WINDOW_DAYS = 90
DRY = "--dry" in sys.argv  # compute + print, skip all DB writes (for pre-migration testing)

CADENCE_DAYS = {"weekly": 7, "monthly": 30, "bimonthly": 60,
                "quarterly": 90, "biannual": 180, "annual": 365, "dormant": None}
SENIORITY = {"decision-maker": 1.0, "influencer": 0.6, "staff": 0.3, "unknown": 0.5}

# Concise Steptoe practice list for the firm-fit judgment (until the live roster scrape lands).
STEPTOE_PRACTICES = (
    "litigation, appellate, white collar, government & regulatory, antitrust, "
    "international trade, IP (patent/trademark/copyright), energy, environment, "
    "financial services, banking & finance, tax, labor & employment, insurance, "
    "data privacy & cybersecurity, telecom/internet/media, M&A/corporate, "
    "life sciences & health care, real estate, bankruptcy"
)

WEIGHTS = {"firm_fit": 0.20, "triggers": 0.40, "relationship": 0.30, "business": 0.10}


def parse_date(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


FIRM_FIT_MAX_AGE = 90      # recompute at least every ~3 months
FIRM_FIT_DUE_AGE = 30      # recompute sooner if a contact is coming due
DUE_SOON_DAYS = 14


def needs_refit(company, due_soon):
    if company.get("firm_fit") is None:
        return True
    ts = parse_date(company.get("firm_fit_updated_at"))
    if ts is None:
        return True
    age = (TODAY - ts).days
    if age >= FIRM_FIT_MAX_AGE:
        return True
    if due_soon and age >= FIRM_FIT_DUE_AGE:
        return True
    return False


USER_PRACTICES = {"litigation", "appeals", "appellate", "appeals & advocacy",
                  "litigation & dispute resolution"}


def firm_fit(client, company, due_soon=False, catalog=None):
    """0-1 fit + short note. Cached on the company; refreshed on a ~3-month cadence
    (sooner if a contact is within DUE_SOON_DAYS of being due). Also picks the best-fit
    practice from the live scraped catalog and, if it's OUTSIDE litigation/appeals,
    stores it as cross_sell_practice for colleague suggestions."""
    if not needs_refit(company, due_soon):
        return float(company["firm_fit"]), company.get("firm_fit_note") or ""
    pick = ""
    if catalog:
        pick = (f' Also choose the SINGLE best-fit practice for this company from this exact '
                f'list (or "Litigation" if litigation/appeals fits best): [{", ".join(catalog[:120])}]. '
                f'Return it verbatim as "practice".')
    prompt = (
        f"Steptoe LLP practices: {STEPTOE_PRACTICES}.\n"
        f"Company: {company['name']} (sector: {company.get('sector') or 'unknown'}).\n"
        f"How well does this company plausibly need Steptoe's services?{pick}\n"
        f'Reply with JSON {{"fit": 0.0-1.0, "note": "<6 words on best-fit practice>"'
        + (', "practice": "<from the list>"' if catalog else '') + '}. JSON only.'
    )
    fit, note, practice = 0.5, "", ""
    try:
        msg = client.messages.create(model=MODEL, max_tokens=160,
                                     messages=[{"role": "user", "content": prompt}])
        text = msg.content[0].text.strip().strip("`")
        if text.startswith("json"):
            text = text[4:]
        data = json.loads(text[text.find("{"):text.rfind("}") + 1])
        fit, note = float(data.get("fit", 0.5)), str(data.get("note", ""))[:120]
        practice = str(data.get("practice", ""))[:120]
    except Exception:
        pass
    cross = practice if practice and practice.strip().lower() not in USER_PRACTICES else None
    if not DRY:
        db.update_company(company["id"], {
            "firm_fit": fit, "firm_fit_note": note,
            "firm_fit_updated_at": dt.datetime.utcnow().isoformat(),
            "cross_sell_practice": cross,
        })
    return fit, note


def main():
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    weights = db.get_config().get("scoring_weights") or {}
    w = {**WEIGHTS, **{k: weights[k] for k in WEIGHTS if k in weights}}

    contacts = db.get_contacts()
    companies = {c["id"]: c for c in db.get_companies_full()}
    catalog = [p["name"] for p in db.get("practice_areas?select=name&status=eq.active&order=name.asc")]
    signals = db.get("signals?select=company_id,type,score_impact,proximity_weight,"
                     "event_date,title,summary&dismissed=eq.false")
    biz = db.get("business_origination?select=contact_id")
    biz_contacts = {b["contact_id"] for b in biz if b.get("contact_id")}

    sig_by_co = {}
    for s in signals:
        sig_by_co.setdefault(s.get("company_id"), []).append(s)

    # Which companies have a contact coming due soon (drives early firm-fit refresh).
    company_due_soon = {}
    for c in contacts:
        interval = CADENCE_DAYS.get(c.get("cadence_tier"))
        if not interval:
            continue
        last = parse_date(c.get("last_contacted_at"))
        days_until = (interval - (TODAY - last).days) if last else -999
        if days_until <= DUE_SOON_DAYS:
            company_due_soon[c.get("company_id")] = True

    for c in contacts:
        co = companies.get(c.get("company_id"))
        # --- relationship: priority + overdue need ---
        p = (c.get("manual_priority") or 3) / 5.0
        interval = CADENCE_DAYS.get(c.get("cadence_tier"))
        last = parse_date(c.get("last_contacted_at"))
        if interval and last:
            need = min((TODAY - last).days / interval, 1.5) / 1.5
        else:
            need = 1.0  # never contacted / no cadence
        sen = SENIORITY.get(c.get("seniority"), 0.5)
        relationship = min(0.5 * p + 0.3 * need + 0.2 * sen, 1.0)

        # --- triggers: recent signals on the company, interest-matched bonus ---
        interests = [i.lower() for i in (c.get("interests") or [])]
        trig, top_sig = 0.0, None
        for s in sig_by_co.get(c.get("company_id"), []):
            ev = parse_date(s.get("event_date")) or TODAY
            age = (TODAY - ev).days
            if age > SIGNAL_WINDOW_DAYS:
                continue
            decay = max(0.0, 1.0 - age / SIGNAL_WINDOW_DAYS)
            contrib = float(s.get("score_impact") or 0) * float(s.get("proximity_weight") or 1) * decay
            text = f"{s.get('title','')} {s.get('summary','')}".lower()
            if interests and any(i in text for i in interests):
                contrib += 0.15
            trig += contrib
            if top_sig is None or contrib > top_sig[0]:
                top_sig = (contrib, s)
        triggers = min(trig, 1.0)

        # --- firm fit (cached, refreshed on cadence) + business-in ---
        ff, ff_note = (firm_fit(client, co, company_due_soon.get(co["id"], False), catalog)
                       if co else (0.5, ""))
        business = 1.0 if c["id"] in biz_contacts else 0.0

        score01 = (w["firm_fit"] * ff + w["triggers"] * triggers
                   + w["relationship"] * relationship + w["business"] * business)
        opp = round(100 * score01)

        # --- rationale ---
        parts = []
        if top_sig and top_sig[0] > 0.1:
            parts.append(f"signal: {top_sig[1].get('title', '')[:60]}")
        if need > 0.7:
            days = (TODAY - last).days if last else None
            parts.append(f"{days}d since contact" if days else "never contacted")
        if ff > 0.6 and ff_note:
            parts.append(f"firm fit: {ff_note}")
        if business:
            parts.append("has sent business")
        rationale = (f"Opportunity {opp} vs your priority {c.get('manual_priority') or '—'}. "
                     + ("Drivers: " + "; ".join(parts) + ". " if parts else "")
                     + "Verify conflicts before pitching.")

        if not DRY:
            db.update_contact(c["id"], {
                "opportunity_score": opp,
                "opportunity_rationale": rationale,
                "score_updated_at": dt.datetime.utcnow().isoformat(),
            })
            db.post("score_snapshots", {
                "contact_id": c["id"], "opportunity_score": opp,
                "components": {"firm_fit": round(ff, 2), "triggers": round(triggers, 2),
                               "relationship": round(relationship, 2), "business": business},
                "rationale": rationale,
            }, prefer="return=minimal")
        print(f"{opp:>3}  {c['name']}  (fit={ff:.2f} trig={triggers:.2f} rel={relationship:.2f})")

    print(f"\nScored {len(contacts)} contacts.")


if __name__ == "__main__":
    main()
