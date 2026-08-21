-- Migration 013: Pitch Reps — a log of pitch/BD "reps" (the work itself), separate
-- from business_origination (business that actually came in through a connection).
-- Run once in the Supabase SQL Editor. Safe to re-run.

create table if not exists pitch_reps (
  id              uuid primary key default gen_random_uuid(),
  date            date not null default current_date,
  partner         text,          -- firm colleague pitched with
  contact_id      uuid references contacts(id) on delete set null,  -- the potential client
  source          text,          -- how it came in (referral, existing client, RFP, event...)
  potential_value numeric,
  description     text,          -- GENERAL matter description only; no confidential detail
  work_type       text check (work_type in ('litigation','cross-sell')),
  outcome         text default 'pending' check (outcome in ('pending','landed','lost')),
  outcome_date    date,          -- when it landed / was lost
  notes           text,
  created_at      timestamptz default now(),
  updated_at      timestamptz default now()
);
create index if not exists pitch_reps_date_idx on pitch_reps (date desc);
create index if not exists pitch_reps_contact_idx on pitch_reps (contact_id, date desc);
