-- Migration 008: per-company negative topics (exclude terms) for the AI news filter.
-- Run once in the Supabase SQL Editor. Safe to re-run.
alter table companies add column if not exists negative_terms text[] default '{}';
