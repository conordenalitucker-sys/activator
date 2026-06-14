-- Migration 011: vacation mode.
-- While on vacation the normal daily/weekend BD emails are suppressed; instead the
-- user gets a "vacation plan" email at the start and a fresh check-in each Monday
-- during the trip. Vacation is active when today (PT) is within [vacation_start,
-- vacation_end] inclusive and both are set. Cadence auto-resumes once vacation_end
-- passes (date-based, no manual step). vacation_last_email_date dedupes sends so the
-- start case and the Monday case never double-send on the same day.

alter table config add column if not exists vacation_start date;
alter table config add column if not exists vacation_end date;
alter table config add column if not exists vacation_last_email_date date;
