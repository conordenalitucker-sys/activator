-- Migration 010: pause outreach to a contact, with a periodic "still paused?" review.
-- Pausing suppresses the contact from the daily plan + email (monitoring continues).
-- pause_review_at = paused_at + 90 days; the dashboard resurfaces the contact for review
-- when that date passes ("keep paused" pushes it +90 days, "resume" clears all of this).

alter table contacts add column if not exists outreach_paused boolean default false;
alter table contacts add column if not exists paused_at date;
alter table contacts add column if not exists pause_review_at date;
alter table contacts add column if not exists pause_reason text;
