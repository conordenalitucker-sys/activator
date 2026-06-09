-- Migration 004: opportunity score storage + cached firm-fit.
-- Run once in the Supabase SQL Editor. Safe to re-run.
alter table contacts  add column if not exists opportunity_score numeric;
alter table contacts  add column if not exists opportunity_rationale text;
alter table contacts  add column if not exists score_updated_at timestamptz;
alter table companies add column if not exists firm_fit numeric;       -- 0-1, Claude-cached
alter table companies add column if not exists firm_fit_note text;     -- why it fits Steptoe
