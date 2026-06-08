# Project Activator

A bespoke, single-user business-development relationship engine for an attorney.
Curates a focused book of contacts, monitors their companies / corporate families /
industries via **public** sources, challenges the user's priority ranking with an
independent opportunity score, and produces a daily/weekly to-do list with draft
(never auto-sent) outreach.

## Status
Design complete; build not yet started. The **canonical design + status log** lives at
`~/Desktop/Activator-Project.txt`. `PLAN.md` here is an earlier copy.

## Core constraints
- **No confidential / representation information** ever enters the system — public
  materials + personal relationship notes only.
- **Draft, never auto-send pitches** (human review per ABA rules).
- The user's **manual priority is never overwritten**; an independent opportunity score
  sits beside it.

## Planned stack
- **Streamlit** dashboard on **Streamlit Community Cloud** (Google-email-restricted login)
- **Supabase** (Postgres) datastore
- **GitHub Actions** cron for monitoring + daily email brief
- **Claude** for drafting, scoring, and cross-sell colleague matching
- Python 3.9-compatible (avoid 3.10+ type-hint syntax)

See the canonical doc for the full data model, monitoring design (4 rings: company +
corporate family + related companies + industry), firm-roster scraping + cross-sell
engine, and build phases.
