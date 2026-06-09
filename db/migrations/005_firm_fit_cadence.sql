-- Migration 005: track when firm-fit was last computed (for the 3-month refresh cadence).
-- Run once in the Supabase SQL Editor. Safe to re-run.
alter table companies add column if not exists firm_fit_updated_at timestamptz;
