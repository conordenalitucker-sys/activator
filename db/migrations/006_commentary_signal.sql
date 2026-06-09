-- Migration 006: allow 'commentary' as a signal type (industry/legal analysis, client alerts).
-- Run once in the Supabase SQL Editor. Safe to re-run.
alter table signals drop constraint if exists signals_type_check;
alter table signals add constraint signals_type_check
  check (type in ('litigation','regulatory','corporate','leadership','news','industry-trend','commentary'));
