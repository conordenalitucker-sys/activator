# Project Activator — Development & Implementation Plan

> **Canonical tracking doc is now `/Users/CTuckerPersonal/Desktop/Activator-Project.txt`** —
> it has the latest design (incl. expanded corporate-family + industry monitoring) and a
> status log. This file is an earlier copy; update the Desktop doc going forward.

A bespoke, single-user business-development relationship engine for an attorney.
Curates a focused book of contacts, monitors their companies/industries via **public**
sources, challenges the user's priority ranking with an independent opportunity score,
and produces a daily/weekly to-do list with draft (never auto-sent) outreach.

---

## 0. Guiding principles & hard constraints

1. **No confidential or representation information ever enters the system.** Notes hold
   public facts + personal relationship context only (family, interests, how you met,
   communication preferences). A visible data-policy banner reinforces this. The tool
   cannot see firm conflicts, so any *pitch* suggestion is appended with
   **"Verify conflicts in the firm's system before pitching."**
2. **Draft, never send pitches.** ABA Model Rules require human review. The tool drafts
   email + LinkedIn copy; the user sends manually and clicks **Log it** to record the touch.
3. **Bespoke and useful**, designed from the requirements — not a clone of existing tools.
4. **Two scores, never overwrite.** The user's manual priority is sacred; the opportunity
   score sits beside it with a written rationale and a delta.
5. **Single user, ~50–200 contacts**, cloud-hosted with login, reachable anywhere.

---

## 1. Recommended architecture & stack

| Layer | Choice | Why |
|---|---|---|
| Dashboard UI | **Streamlit** (Python) | Fastest path to a clean single-user product: sliders, per-feed toggles, buttons, tables, progress bars all built in. Matches Python comfort. |
| Hosting | **Streamlit Community Cloud** | Free; connects to a GitHub repo; **viewer access restricted to your Google email** = "cloud login, access anywhere" with zero auth code. |
| Data + auth backend | **Supabase** (managed Postgres) | Free tier, accessible anywhere, real SQL for 200 contacts + signals history. (Auth handled at the Streamlit Cloud layer; Supabase is the datastore.) |
| Scheduled jobs | **GitHub Actions cron** (free) | Runs the monitoring pipeline + sends the daily email on a schedule, writing results to Supabase. No server to babysit. |
| AI | **Anthropic Claude** (existing key), with prompt caching | Drafts outreach, computes firm-fit + rationale, powers the suggestions engine. |
| Sheet sync | **gspread + Google service account** | Two-way-ish sync between Supabase and a Google Sheet for quick mobile edits. |
| Email brief | **Gmail SMTP** (existing app password) | Daily brief delivery. |

**Alternative considered:** FastAPI + React + Render/Railway for a more polished, multi-user
product. Rejected for now — heavier to build and unnecessary for a solo 50–200 contact tool.
The data model below is framework-agnostic, so this remains an upgrade path.

**Estimated cost:** ~$0/mo infra (free tiers) + Claude usage (a few dollars/mo at this scale)
+ optional paid data feed later.

---

## 2. Data model (Postgres)

- **contacts** — id, name, title, company_id, email, linkedin_url, phone, location,
  role/practice, seniority/decision_power (enum), comm_preference (email/call/LinkedIn/in-person),
  personal_notes (public/relationship only), how_we_met, manual_priority (1–5),
  cadence_tier (weekly/monthly/quarterly/biannual/annual/dormant), tags[], last_contacted_at,
  created_at, updated_at.
- **companies** — id, name, sector/industry, size, website, sec_cik, stock_ticker,
  monitored_feeds (jsonb of per-company toggles), notes.
- **interactions** — id, contact_id, date, type (personal/pitch/legal-update/industry-check/hello),
  channel (email/LinkedIn/call/in-person), notes, logged_via (button/manual).
- **signals** — id, company_id, type (litigation/regulatory/corporate/leadership/news),
  event_date, source, url, title, summary, score_impact, surfaced (bool), dismissed (bool).
- **feeds** — id, scope (global/company), kind (news/court/sec/regulatory/industry), config,
  enabled (the dashboard toggles).
- **daily_plan** — id, plan_date, contact_id, reason, suggested_touch_type, draft_email,
  draft_linkedin, status (pending/done/snoozed/skipped).
- **suggestions** — id, kind (new-contact/new-feed/data-update/priority-flag), target_ref,
  body, status (new/accepted/dismissed) — the proactive "anticipate & suggest" inbox.
- **config** — firm practice areas[], daily_goal_count, cadence-tier interval definitions,
  scoring weights, user profile (firm name, jurisdictions).
- **score_snapshots** — contact_id, date, opportunity_score, component breakdown, rationale.

---

## 3. Monitoring layer (public sources only)

Per-company and global, each toggleable on the dashboard:

- **News** — Google News RSS queries per company + per industry (free); GDELT/NewsAPI optional.
- **Courts** — **CourtListener REST API + Alerts** (free) for new federal dockets & opinions
  naming the company; RECAP for documents. (PACER full dockets = optional paid upgrade.)
- **SEC** — **EDGAR** full-text search API + company filing RSS (free): 8-Ks, litigation
  disclosures, leadership changes.
- **Regulatory** — Federal Register API + regulations.gov + agency RSS (free): enforcement,
  rulemakings affecting the sector.
- **Industry/trade** — user-configurable RSS feeds per sector.

Pipeline (GitHub Actions, e.g. nightly): pull each enabled feed → normalize → dedupe →
Claude classifies & summarizes each hit into a **signal** with a type and a `score_impact`
→ write to Supabase. **Add-feed toggle** on the dashboard turns sources on/off without redeploy.

---

## 4. Priority engine (two scores side by side)

- **Manual priority** — 1–5 slider, user-owned, never changed by the system.
- **Opportunity score** — 0–100, computed from four weighted signals (weights configurable):
  1. **Firm practice-area fit** — Claude estimates the company's likely legal needs vs. the
     firm's practice areas (from config).
  2. **Litigation & regulatory triggers** — recency/severity of court & regulatory signals.
  3. **Corporate & leadership events** — M&A, funding, new GC/C-suite, expansions, layoffs.
  4. **Relationship strength & recency** — seniority/decision power + warmth + how recent.
- **Display** — side by side with the **delta** and a one-paragraph **rationale** ("Opportunity
  82 vs. your 3 — new securities suit filed last week + you haven't talked in 5 months").
- Pitch-related rationale always appends the **conflict-check reminder**.

---

## 5. Cadence & daily/weekly engine

- Each contact has a **cadence tier** → target interval (e.g., monthly = 30d).
- **Daily list (3–5, value-first):** contacts overdue per cadence + contacts with fresh
  triggers, ranked, capped. Each item carries a **suggested touch type** (hello / relevant
  legal update / industry check-in / congratulations / pitch) and draft copy.
- **Daily goal + progress bar:** `daily_goal_count` (default 4); a **% done** bar on the
  dashboard fills as you click **Log it**.
- **Weekly review:** a fuller Monday scan of the whole book — who's drifting, biggest
  opportunity-score/manual-priority gaps, dormant high-value contacts.
- **Log it button** records the interaction (type + channel + date) and **resets the cadence
  clock**, updating `last_contacted_at`.

---

## 6. Outreach drafting

For each daily item, Claude drafts **two versions** (email + LinkedIn) grounded in: stored
public facts, the triggering signal, the contact's comm preference and personal notes, and the
suggested touch type. Copy-ready, you edit and send yourself, then **Log it**. Pitches carry the
conflict reminder. Tone/length configurable per contact preference.

---

## 7. Suggestions engine ("anticipate & suggest")

A periodic Claude pass that proposes additions for you to accept/dismiss in a Suggestions inbox:
- New people to add (e.g., a company's newly announced GC).
- New feeds/sources worth monitoring for a contact's sector.
- Data updates (title/job changes detected in signals).
- Priority flags ("you're under-investing in this high-opportunity contact").

---

## 8. Build phases

- **Phase 0 — Setup:** create Supabase project + schema; GitHub repo; Streamlit Cloud app with
  Google-email access restriction; secrets (Anthropic key, Gmail, Supabase, Google service acct).
  *Needs from you:* Excel column headers / sample CSV, firm practice areas, jurisdictions.
- **Phase 1 — Data + import:** schema migrations; CSV importer mapped to your Excel; Google
  Sheet two-way sync.
- **Phase 2 — Dashboard core:** contact list/profile CRUD, manual-priority slider, cadence
  tiers, tags, search/filter.
- **Phase 3 — Monitoring:** feed framework + dashboard toggles; CourtListener + SEC + News +
  regulatory adapters; signals pipeline on GitHub Actions cron.
- **Phase 4 — Scoring:** opportunity-score engine + Claude firm-fit & rationale; two-score UI;
  snapshots/history.
- **Phase 5 — Daily/weekly engine:** daily queue, goal + % progress bar, Log it button, weekly
  review view.
- **Phase 6 — Outreach drafting:** email + LinkedIn draft generation; copy + Log flow.
- **Phase 7 — Daily email brief:** scheduled morning email mirroring the day's queue.
- **Phase 8 — Suggestions engine + polish:** suggestions inbox; data-policy banner; conflict
  reminders; QA with your real ~50–200 contacts.

**Suggested first milestone (end-to-end thin slice):** Phases 0–2 + a single monitoring source
(CourtListener) + the two-score display on the dashboard with real seeded contacts — proves the
core loop before building breadth.

---

## 9. Open inputs needed from you

- Current Excel column headers (or a de-identified sample row).
- Your firm's practice areas + primary jurisdictions (for firm-fit scoring).
- Confirm accounts to use: Anthropic API key, Gmail sender, a Google account for Streamlit
  Cloud login, and willingness to create a free Supabase account.
- Default daily goal (touches/day) — proposed: 4.
