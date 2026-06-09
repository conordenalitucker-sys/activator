-- Migration 002: extend touch types + channels; add business-origination tracking.
-- Run once in the Supabase SQL Editor. Safe to re-run.

alter table interactions drop constraint if exists interactions_type_check;
alter table interactions add constraint interactions_type_check
  check (type in ('personal','pitch','legal-update','industry-check','hello','client-work','other'));

alter table interactions drop constraint if exists interactions_channel_check;
alter table interactions add constraint interactions_channel_check
  check (channel in ('email','LinkedIn','call','in-person','event','text','other'));

-- Business that came IN through a connection (origination / referral tracking).
create table if not exists business_origination (
  id          uuid primary key default gen_random_uuid(),
  contact_id  uuid references contacts(id) on delete set null,
  date        date not null default current_date,
  description text,
  est_value   numeric,
  created_at  timestamptz default now()
);
create index if not exists business_origination_contact_idx
  on business_origination (contact_id, date desc);
