-- Migration 001: add per-contact interests (topics they care about).
-- Run once in the Supabase SQL Editor. Safe to re-run.
alter table contacts add column if not exists interests text[] default '{}';
