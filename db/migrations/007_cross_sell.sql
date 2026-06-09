-- Migration 007: store each company's best-fit practice (for cross-sell colleague suggestions).
-- Run once in the Supabase SQL Editor. Safe to re-run.
alter table companies add column if not exists cross_sell_practice text;
