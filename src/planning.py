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
# A paused contact is suppressed from routine cadence nudges, but a live signal at/above
# this impact (score_impact * proximity_weight, within the window) is a "big development"
# worth interrupting the pause for, so the contact still surfaces as an opportunity.
BIG_SIGNAL_IMPACT = 0.5


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


def has_big_signal(c, sig_by_company, today, threshold=BIG_SIGNAL_IMPACT):
    """True if the contact's company has a live (in-window) signal whose impact
    (score_impact * proximity_weight) is big enough to interrupt a pause."""
    for s in sig_by_company.get(c.get("company_id"), []):
        ev = parse_date(s.get("event_date")) or today
        if (today - ev).days > SIGNAL_WINDOW_DAYS:
            continue
        impact = float(s.get("score_impact") or 0) * float(s.get("proximity_weight") or 1)
        if impact >= threshold:
            return True
    return False


def actionable_signal(c, sig_by_company, today, threshold=BIG_SIGNAL_IMPACT):
    """The contact's strongest live signal that is BOTH highly relevant AND fresh,
    or None. A signal only justifies an off-cadence touch if (a) it's high-impact
    (score_impact * proximity_weight >= threshold) and (b) it broke AFTER you last
    reached out. Once you've contacted someone, the same news stops re-surfacing
    them day after day — only genuinely newer developments do."""
    last = parse_date(c.get("last_contacted_at"))
    best = None
    for s in sig_by_company.get(c.get("company_id"), []):
        ev = parse_date(s.get("event_date")) or today
        if (today - ev).days > SIGNAL_WINDOW_DAYS:
            continue
        if last is not None and ev <= last:
            continue  # you've already had the chance to act on this
        impact = float(s.get("score_impact") or 0) * float(s.get("proximity_weight") or 1)
        if impact >= threshold and (best is None or impact > best[0]):
            best = (impact, s)
    return best[1] if best else None


def resolve_weights(weights):
    return {**DEFAULT_WEIGHTS,
            **{k: weights[k] for k in DEFAULT_WEIGHTS if weights and k in weights}}


# --- referrals / business-in ----------------------------------------------
# How long after the last touch a referral source is considered to be going cold
# and worth a proactive "keep warm" nudge.
REFERRAL_KEEP_WARM_DAYS = 60


def index_referrals(rows):
    """Group business_origination rows into {contact_id: [rows]}."""
    out = {}
    for b in (rows or []):
        cid = b.get("contact_id")
        if cid:
            out.setdefault(cid, []).append(b)
    return out


def referral_strength(rows, today):
    """0..1 strength of a contact as a referral source, from their origination
    history: more, larger (actual billings preferred over estimate), and more
    recent referrals score higher. Decays over ~2 years with a floor so an old
    referral still counts for something."""
    if not rows:
        return 0.0
    total = 0.0
    for b in rows:
        d = parse_date(b.get("date")) or today
        age = max((today - d).days, 0)
        recency = max(0.2, 1.0 - age / 730.0)
        val = float(b.get("actual_value") or b.get("est_value") or 0)
        val_w = min(val / 100000.0, 1.0)            # $100k+ realized = full value weight
        total += recency * (0.6 + 0.4 * val_w)
    return min(total, 1.0)


def referral_due(c, biz_by_contact, today, keep_warm_days=REFERRAL_KEEP_WARM_DAYS):
    """True if this contact has sent business and has gone cold (never contacted,
    or not contacted within keep_warm_days) — i.e. a referral source the engine
    should proactively recommend reaching out to. Resets once you log a touch."""
    rows = (biz_by_contact or {}).get(c.get("id"))
    if not rows:
        return False
    ds = days_since(c, today)
    return ds is None or ds >= keep_warm_days


# --- pitch reps -------------------------------------------------------------
# How long a still-open pitch counts as a LIVE pursuit before it's treated as stale.
PITCH_ACTIVE_DAYS = 180
# A pursuit at or above this size carries full value weight in the pitch component.
PITCH_FULL_VALUE = 250000.0


def index_pitches(rows):
    """Group pitch_reps rows into {contact_id: [rows]}."""
    out = {}
    for r in (rows or []):
        cid = r.get("contact_id")
        if cid:
            out.setdefault(cid, []).append(r)
    return out


def pitch_strength(rows, today):
    """0..1 strength of a contact as an ACTIVE pitch target, from your pitch reps.

    An open pitch is the strongest form of live opportunity — you are already in
    motion — and decays to nothing over ~6 months as it goes stale. A landed pitch
    counts for less here (the resulting work usually shows up in business_origination
    too, and this shouldn't double-count it); a lost pitch leaves a small residual for
    a year, because you invested in the relationship and it can come back."""
    if not rows:
        return 0.0
    total = 0.0
    for r in rows:
        d = parse_date(r.get("date")) or today
        age = max((today - d).days, 0)
        val_w = min(float(r.get("potential_value") or 0) / PITCH_FULL_VALUE, 1.0)
        size = 0.6 + 0.4 * val_w
        outcome = r.get("outcome") or "pending"
        if outcome == "pending":
            total += max(0.0, 1.0 - age / PITCH_ACTIVE_DAYS) * size
        elif outcome == "landed":
            total += 0.5 * max(0.2, 1.0 - age / 730.0) * size
        else:  # lost
            total += max(0.0, 1.0 - age / 365.0) * 0.15
    return min(total, 1.0)


def open_pitches(rows):
    return [r for r in (rows or []) if (r.get("outcome") or "pending") == "pending"]


def compute_opportunity(c, company, signals, biz_by_contact, weights, today,
                        pitch_by_contact=None):
    """Return (score 0-100, rationale, components, top_signal). Uses the company's
    CACHED firm_fit (firm_fit refresh is score.py's job).

    biz_by_contact is {contact_id: [business_origination rows]} (preferred) so the
    business component scales with referral count/value/recency. A bare set/list of
    contact ids is still accepted for backward compatibility (binary credit).

    pitch_by_contact is {contact_id: [pitch_reps rows]}; pitch work you've already put
    in — especially a still-open pursuit — adds to the same business component."""
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
    if isinstance(biz_by_contact, dict):
        ref_rows = biz_by_contact.get(c.get("id")) or []
        referrals = referral_strength(ref_rows, today)
        ref_n = len(ref_rows)
    else:  # legacy: a set/list of contact ids -> binary credit
        referrals = 1.0 if c.get("id") in (biz_by_contact or set()) else 0.0
        ref_n = 1 if referrals else 0

    pitch_rows = (pitch_by_contact or {}).get(c.get("id")) or []
    pitches = pitch_strength(pitch_rows, today)
    open_n = len(open_pitches(pitch_rows))
    # One "business" component covering both sides of the same thing: business they've
    # sent you, and business you're actively chasing with them.
    business = min(referrals + pitches, 1.0)

    score01 = (w["firm_fit"] * ff + w["triggers"] * triggers
               + w["relationship"] * relationship + w["business"] * business)

    # Attention bonus: a relationship you've flagged as drifting (and are NOT ok with)
    # gets pulled up the daily list. "ok" relationships add nothing.
    traj, traj_ok = c.get("trajectory"), c.get("trajectory_ok")
    attention = ({"apart": 0.20, "same": 0.10, "closer": 0.05}.get(traj, 0.0)
                 if traj_ok is False else 0.0)
    opp = round(100 * min(score01 + attention, 1.0))

    parts = []
    if attention > 0:
        parts.append(f"⚠️ drifting ({traj}), not ok — needs attention")
    if top and top[0] > 0.1:
        parts.append(f"signal: {(top[1].get('title') or '')[:60]}")
    if need > 0.7:
        ds = (today - last).days if last else None
        parts.append(f"{ds}d since contact" if ds is not None else "never contacted")
    if ff > 0.6 and ff_note:
        parts.append(f"firm fit: {ff_note}")
    if referrals > 0:
        parts.append("referral source" + (f" ({ref_n} referrals)" if ref_n > 1
                                           else " (sent business)"))
    if pitches > 0:
        parts.append(f"open pitch ({open_n})" if open_n else "pitched before")
    rationale = (f"Opportunity {opp} vs your priority {c.get('manual_priority') or '—'}. "
                 + ("Drivers: " + "; ".join(parts) + ". " if parts else "")
                 + "Verify conflicts before pitching.")
    comps = {"firm_fit": round(ff, 2), "triggers": round(triggers, 2),
             "relationship": round(relationship, 2), "business": round(business, 2),
             "referrals": round(referrals, 2), "pitches": round(pitches, 2)}
    return opp, rationale, comps, (top[1] if top else None)


def select_daily_plan(contacts, sig_by_company, goal, today, min_cadence=MIN_CADENCE,
                      biz_by_contact=None):
    """Unified pick logic for BOTH the dashboard Today list and the daily email.
    Returns (opp_picks, cad_picks):
      opp_picks = contacts with an ACTIONABLE signal (highly relevant AND newer than
                  their last contact) OR a referral source going cold, ranked by
                  opportunity score. The actionable-signal bar also overrides a pause.
      cad_picks = overdue contacts (PREFER no-signal pure-cadence), >= min_cadence,
                  so news can't crowd out relationship-keeping.

    A contact who is neither overdue on cadence, nor has fresh high-impact news, nor
    is a cooling referral source is NOT surfaced — so people you've just reached out
    to don't reappear day after day.
    """
    def has_sig(c):
        return len(sig_by_company.get(c.get("company_id"), [])) > 0

    def paused(c):
        return bool(c.get("outreach_paused"))

    def actionable(c):
        return actionable_signal(c, sig_by_company, today) is not None

    def referral(c):
        # Keep referral sources warm, but respect an explicit pause.
        return not paused(c) and referral_due(c, biz_by_contact, today)

    # Opportunity branch: surface a contact off-cadence for a fresh, highly relevant
    # development (impact >= BIG_SIGNAL_IMPACT and dated after the last contact) — the
    # same bar that interrupts a pause — OR because they've sent business and are going
    # cold (a referral source worth proactively nurturing). Ranked by opportunity score.
    opp_pool = sorted([c for c in contacts if actionable(c) or referral(c)],
                      key=lambda c: (-opp_score(c), -(c.get("manual_priority") or 0)))
    max_opp = max(goal - min_cadence, 0)
    opp_picks = opp_pool[:max_opp]
    picked = {c["id"] for c in opp_picks}

    # Cadence branch: only contacts actually overdue per their cadence, never paused.
    overdue_rest = [c for c in contacts
                    if not paused(c) and is_overdue(c, today) and c["id"] not in picked]
    by_overdue = lambda c: (-(overdue_days(c, today) or 0), -(c.get("manual_priority") or 0))
    pure = sorted([c for c in overdue_rest if not has_sig(c)], key=by_overdue)
    withsig = sorted([c for c in overdue_rest if has_sig(c)], key=by_overdue)
    cad_picks = (pure + withsig)[:max(min_cadence, goal - len(opp_picks))]
    return opp_picks, cad_picks
