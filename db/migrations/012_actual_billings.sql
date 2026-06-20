-- Migration 012: track ACTUAL billings alongside the estimated value of
-- originated business. Run once in the Supabase SQL Editor. Safe to re-run.
-- Existing rows keep their est_value untouched; actual_value starts null.

alter table business_origination add column if not exists actual_value numeric;
