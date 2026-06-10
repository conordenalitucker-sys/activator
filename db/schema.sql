-- Project Activator — database schema (Supabase / Postgres)
-- Run this once in the Supabase SQL Editor. Safe to re-run (IF NOT EXISTS).

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- config: single-row app configuration
-- ---------------------------------------------------------------------------
create table if not exists config (
  id                   int primary key default 1,
  firm_name            text default 'Steptoe LLP',
  user_expertise       text[] default array['Litigation','Appeals & Advocacy'],
  jurisdictions        text[] default '{}',
  daily_goal_count     int  default 4,
  daily_goal_minutes   int,
  roster_scrape_cadence text default 'weekly',
  scoring_weights      jsonb default '{"firm_fit":0.10,"triggers":0.40,"relationship":0.35,"business":0.15}'::jsonb,
  updated_at           timestamptz default now(),
  constraint config_singleton check (id = 1)
);
insert into config (id) values (1) on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- companies
-- ---------------------------------------------------------------------------
create table if not exists companies (
  id              uuid primary key default gen_random_uuid(),
  name            text not null,
  sector          text,
  sic_naics       text,
  size            text,
  website         text,
  sec_cik         text,
  stock_ticker    text,
  monitored_feeds jsonb default '{}'::jsonb,
  industries      text[] default '{}',
  watch_terms     text[] default '{}',
  negative_terms  text[] default '{}',   -- exclude topics: AI drops news primarily about these
  home_state      text,
  track_state_regulators boolean default false,
  segment_focus   text,        -- narrow big parents to the contact's unit, e.g. "Amazon Studios"
  jurisdiction_focus text,     -- e.g. "California" — narrows court/news monitoring
  firm_fit        numeric,     -- 0-1, Claude-cached: how well the company fits Steptoe's practices
  firm_fit_note   text,
  firm_fit_updated_at timestamptz,  -- refreshed on a ~3-month cadence (sooner if a contact is due)
  cross_sell_practice text,    -- best-fit practice OUTSIDE litigation/appeals (for colleague suggestions)
  notes           text,
  created_at      timestamptz default now(),
  updated_at      timestamptz default now()
);
create unique index if not exists companies_name_key on companies (lower(name));

-- ---------------------------------------------------------------------------
-- contacts
-- ---------------------------------------------------------------------------
create table if not exists contacts (
  id               uuid primary key default gen_random_uuid(),
  name             text not null,
  title            text,
  company_id       uuid references companies(id) on delete set null,
  email            text,
  linkedin_url     text,
  phone            text,
  location         text,
  role_practice    text,
  seniority        text check (seniority in ('decision-maker','influencer','staff','unknown')),
  comm_preference  text check (comm_preference in ('email','call','LinkedIn','in-person','unknown')),
  personal_notes   text,             -- PUBLIC / relationship info only; NO confidential matter info
  how_we_met       text,
  contact_type     text check (contact_type in ('client','past-client','prospect','professional','referral','friend')),
  manual_priority  int  check (manual_priority between 1 and 5),
  opportunity_score numeric,          -- 0-100, computed by src/score.py
  opportunity_rationale text,
  score_updated_at timestamptz,
  priority_color   text check (priority_color in ('Green','Blue','Purple')),
  cadence_tier     text check (cadence_tier in ('weekly','monthly','bimonthly','quarterly','biannual','annual','dormant')),
  tags             text[] default '{}',
  interests        text[] default '{}',   -- topics they care about (e.g. first amendment, sports betting)
  last_contacted_at timestamptz,
  created_at       timestamptz default now(),
  updated_at       timestamptz default now()
);

-- ---------------------------------------------------------------------------
-- entities: corporate-family & industry graph hung off a primary company
-- ---------------------------------------------------------------------------
create table if not exists entities (
  id                 uuid primary key default gen_random_uuid(),
  name               text not null,
  type               text check (type in ('primary','parent','subsidiary','affiliate','peer-competitor','customer-supplier','co-defendant','industry')),
  related_company_id uuid references companies(id) on delete cascade,
  relationship_note  text,
  proximity_weight   numeric default 1.0,
  sec_cik            text,
  source             text,
  enabled            boolean default true,
  created_at         timestamptz default now()
);

-- ---------------------------------------------------------------------------
-- daily_plan: the curated to-do queue (declared before interactions for FK)
-- ---------------------------------------------------------------------------
create table if not exists daily_plan (
  id                  uuid primary key default gen_random_uuid(),
  plan_date           date not null,
  contact_id          uuid references contacts(id) on delete cascade,
  reason              text,
  suggested_touch_type text,
  draft_email         text,
  draft_linkedin      text,
  status              text default 'pending' check (status in ('pending','done','snoozed','skipped')),
  first_surfaced_date date default current_date,
  rolled_over_count   int default 0,
  actual_touch_type   text,
  actual_channel      text,
  duration_minutes    int,
  completed_at        timestamptz,
  created_at          timestamptz default now()
);
create index if not exists daily_plan_date_idx on daily_plan (plan_date, status);

-- ---------------------------------------------------------------------------
-- interactions: logged touches (suggested or ad-hoc)
-- ---------------------------------------------------------------------------
create table if not exists interactions (
  id              uuid primary key default gen_random_uuid(),
  contact_id      uuid references contacts(id) on delete cascade,
  date            date not null default current_date,
  type            text check (type in ('personal','pitch','legal-update','industry-check','hello','client-work','other')),
  channel         text check (channel in ('email','LinkedIn','call','in-person','event','text','other')),
  notes           text,
  duration_minutes int,
  was_suggested   boolean default false,
  suggested_type  text,
  daily_plan_id   uuid references daily_plan(id) on delete set null,
  logged_via      text default 'manual' check (logged_via in ('button','manual')),
  created_at      timestamptz default now()
);
create index if not exists interactions_contact_idx on interactions (contact_id, date desc);

-- ---------------------------------------------------------------------------
-- signals: monitoring hits across the 4 rings
-- ---------------------------------------------------------------------------
create table if not exists signals (
  id              uuid primary key default gen_random_uuid(),
  entity_id       uuid references entities(id) on delete set null,
  company_id      uuid references companies(id) on delete cascade,
  type            text check (type in ('litigation','regulatory','corporate','leadership','news','industry-trend','commentary')),
  relationship    text,
  proximity_weight numeric default 1.0,
  event_date      date,
  source          text,
  url             text,
  title           text,
  summary         text,
  score_impact    numeric default 0,
  surfaced        boolean default false,
  dismissed       boolean default false,
  created_at      timestamptz default now()
);
create index if not exists signals_company_idx on signals (company_id, event_date desc);
create unique index if not exists signals_dedupe_key on signals (company_id, coalesce(url,''), coalesce(title,''));

-- ---------------------------------------------------------------------------
-- feeds: monitoring source toggles
-- ---------------------------------------------------------------------------
create table if not exists feeds (
  id          uuid primary key default gen_random_uuid(),
  scope       text check (scope in ('global','company','sector')),
  kind        text check (kind in ('news','court','sec','regulatory','industry')),
  config      jsonb default '{}'::jsonb,
  enabled     boolean default true,
  company_id  uuid references companies(id) on delete cascade,
  created_at  timestamptz default now()
);

-- ---------------------------------------------------------------------------
-- suggestions: proactive "anticipate & suggest" inbox
-- ---------------------------------------------------------------------------
create table if not exists suggestions (
  id          uuid primary key default gen_random_uuid(),
  kind        text check (kind in ('new-contact','new-feed','new-entity','data-update','priority-flag')),
  target_ref  text,
  body        text,
  status      text default 'new' check (status in ('new','accepted','dismissed')),
  created_at  timestamptz default now()
);

-- ---------------------------------------------------------------------------
-- firm roster (scraped from steptoe.com)
-- ---------------------------------------------------------------------------
create table if not exists practice_areas (
  id                uuid primary key default gen_random_uuid(),
  name              text not null,
  parent_group      text,
  url               text,
  description       text,
  chairs            text[] default '{}',
  first_seen_scrape timestamptz default now(),
  last_seen_scrape  timestamptz default now(),
  status            text default 'active' check (status in ('active','retired'))
);
create unique index if not exists practice_areas_name_key on practice_areas (lower(name));

create table if not exists attorneys (
  id                  uuid primary key default gen_random_uuid(),
  name                text not null,
  title               text,
  offices             text[] default '{}',
  email               text,
  bio_url             text,
  industries          text[] default '{}',
  status              text default 'active' check (status in ('active','departed')),
  first_seen_scrape   timestamptz default now(),
  last_seen_scrape    timestamptz default now(),
  departed_detected_at timestamptz
);
create unique index if not exists attorneys_bio_key on attorneys (coalesce(bio_url, lower(name)));

create table if not exists attorney_practices (
  attorney_id      uuid references attorneys(id) on delete cascade,
  practice_area_id uuid references practice_areas(id) on delete cascade,
  primary key (attorney_id, practice_area_id)
);

-- ---------------------------------------------------------------------------
-- score_snapshots: opportunity-score history
-- ---------------------------------------------------------------------------
create table if not exists score_snapshots (
  id                uuid primary key default gen_random_uuid(),
  contact_id        uuid references contacts(id) on delete cascade,
  date              date not null default current_date,
  opportunity_score numeric,
  components        jsonb,
  rationale         text,
  created_at        timestamptz default now()
);
create index if not exists score_snapshots_contact_idx on score_snapshots (contact_id, date desc);

-- ---------------------------------------------------------------------------
-- business_origination: business that came IN through a connection
-- ---------------------------------------------------------------------------
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
