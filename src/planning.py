"""Shared planning + scoring core for Project Activator.

ONE place for: cadence math, the opportunity-score formula, and the daily-plan
selection — used by score.py (nightly), app.py (dashboard Today + "Rescore now"),
and daily_email.py — so every surface agrees on who to contact and why.

compute_opportunity() uses the company's CACHED firm_fit (no Claude), so the dashboard
can recompute instantly; score.py refreshes firm_fit (Claude) before calling it.
Python 3.9 compatible.
"""
from __future__ import annotations

import datetime as dt

CADENCE_DAYS = {"weekly": 7, "monthly": 30, "bimonthly": 60,
                "quarterly": 90, "biannual": 180, "annual": 365, "dormant": None}
SENIORITY = {"decision-maker": 1.0, "influencer": 0.6, "staff": 0.3, "unknown": 0.5}
SIGNAL_WINDOW_DAYS = 90
DEFAULT_WEIGHTS = {"firm_fit": 0.10, "triggers": 0.40, "relationship": 0.35, "business": 0.15}
MIN_CADENCE = 2


def parse_date(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


def cadence_interval(c):
    return CADENCE_DAYS.get(c.get("cadence_tier"))


def days_since(c, today):
    d = parse_date(c.get("last_contacted_at"))
    return (today - d).days if d else None


def overdue_days(c, today):
    iv = cadence_interval(c)
    if iv is None:
        return None
    ds = days_since(c, today)
    return (ds - iv) if ds is not None else 10 ** 9  # never contacted -> very overdue


def is_overdue(c, today):
    od = overdue_days(c, today)
    return od is not None and od >= 0


def opp_score(c):
    return c.get("opportunity_score") or 0


def resolve_weights(weights):
    return {**DEFAULT_WEIGHTS,
            **{k: weights[k] for k in DEFAULT_WEIGHTS if weights and k in weights}}


def compute_opportunity(c, company, signals, biz_ids, weights, today):
    """Return (score 0-100, rationale, components, top_signal). Uses the company's
    CACHED firm_fit (firm_fit refresh is score.py's job)."""
    w = resolve_weights(weights)
    p = (c.get("manual_priority") or 3) / 5.0
    iv = cadence_interval(c)
    last = parse_date(c.get("last_contacted_at"))
    need = (min((today - last).days / iv, 1.5) / 1.5) if (iv and last) else 1.0
    sen = SENIORITY.get(c.get("seniority"), 0.5)
    relationship = min(0.5 * p + 0.3 * need + 0.2 * sen, 1.0)

    interests = [i.lower() for i in (c.get("interests") or [])]
    trig, top = 0.0, None
    for s in signals:
        ev = parse_date(s.get("event_date")) or today
        age = (today - ev).days
        if age > SIGNAL_WINDOW_DAYS:
            continue
        decay = max(0.0, 1.0 - age / SIGNAL_WINDOW_DAYS)
        contrib = float(s.get("score_impact") or 0) * float(s.get("proximity_weight") or 1) * decay
        text = f"{s.get('title', '')} {s.get('summary', '')}".lower()
        if interests and any(i in text for i in interests):
            contrib += 0.15
        trig += contrib
        if top is None or contrib > top[0]:
            top = (contrib, s)
    triggers = min(trig, 1.0)

    ff = float(company["firm_fit"]) if company and company.get("firm_fit") is not None else 0.5
    ff_note = (company or {}).get("firm_fit_note") or ""
    business = 1.0 if c.get("id") in biz_ids else 0.0

    score01 = (w["firm_fit"] * ff + w["triggers"] * triggers
               + w["relationship"] * relationship + w["business"] * business)
    opp = round(100 * score01)

    parts = []
    if top and top[0] > 0.1:
        parts.append(f"signal: {(top[1].get('title') or '')[:60]}")
    if need > 0.7:
        ds = (today - last).days if last else None
        parts.append(f"{ds}d since contact" if ds is not None else "never contacted")
    if ff > 0.6 and ff_note:
        parts.append(f"firm fit: {ff_note}")
    if business:
        parts.append("has sent business")
    rationale = (f"Opportunity {opp} vs your priority {c.get('manual_priority') or '—'}. "
                 + ("Drivers: " + "; ".join(parts) + ". " if parts else "")
                 + "Verify conflicts before pitching.")
    comps = {"firm_fit": round(ff, 2), "triggers": round(triggers, 2),
             "relationship": round(relationship, 2), "business": business}
    return opp, rationale, comps, (top[1] if top else None)


def select_daily_plan(contacts, sig_by_company, goal, today, min_cadence=MIN_CADENCE):
    """Unified pick logic for BOTH the dashboard Today list and the daily email.
    Returns (opp_picks, cad_picks):
      opp_picks = contacts with a live signal, ranked by opportunity score.
      cad_picks = overdue contacts (PREFER no-signal pure-cadence), >= min_cadence,
                  so news can't crowd out relationship-keeping.
    """
    def has_sig(c):
        return len(sig_by_company.get(c.get("company_id"), [])) > 0

    opp_pool = sorted([c for c in contacts if has_sig(c)],
                      key=lambda c: (-opp_score(c), -(c.get("manual_priority") or 0)))
    max_opp = max(goal - min_cadence, 0)
    opp_picks = opp_pool[:max_opp]
    picked = {c["id"] for c in opp_picks}

    overdue_rest = [c for c in contacts if is_overdue(c, today) and c["id"] not in picked]
    by_overdue = lambda c: (-(overdue_days(c, today) or 0), -(c.get("manual_priority") or 0))
    pure = sorted([c for c in overdue_rest if not has_sig(c)], key=by_overdue)
    withsig = sorted([c for c in overdue_rest if has_sig(c)], key=by_overdue)
    cad_picks = (pure + withsig)[:max(min_cadence, goal - len(opp_picks))]
    return opp_picks, cad_picks
