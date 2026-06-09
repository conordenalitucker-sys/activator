-- Migration 003: per-company monitoring controls.
-- Run once in the Supabase SQL Editor. Safe to re-run.
alter table companies add column if not exists industries text[] default '{}';
alter table companies add column if not exists watch_terms text[] default '{}';
alter table companies add column if not exists home_state text;
alter table companies add column if not exists track_state_regulators boolean default false;
alter table companies add column if not exists segment_focus text;      -- narrow big parents, e.g. "Amazon Studios"
alter table companies add column if not exists jurisdiction_focus text;  -- e.g. "California" — narrows courts/news
