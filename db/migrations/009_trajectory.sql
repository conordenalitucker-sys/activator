-- Migration 009: relationship trajectory + whether the user is OK with it.
-- Captured per touch (interactions) and mirrored as the contact's current state.
-- Run once in the Supabase SQL Editor. Safe to re-run.
alter table interactions add column if not exists trajectory text
  check (trajectory in ('closer', 'same', 'apart'));
alter table interactions add column if not exists trajectory_ok boolean;

alter table contacts add column if not exists trajectory text
  check (trajectory in ('closer', 'same', 'apart'));
alter table contacts add column if not exists trajectory_ok boolean;
