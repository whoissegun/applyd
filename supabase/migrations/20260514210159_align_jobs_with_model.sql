-- Align public.jobs with the existing Pydantic Job model and the actual
-- discovery/enrichment flow:
--   - locations is a list, not free text
--   - last_seen_at + active are needed for staleness detection
--   - fetch_tier + fetch_error are useful diagnostics on the row
--
-- This runs after 20260514205629_initial_schema.sql.

alter table public.jobs
  add column locations    text[]      not null default '{}',
  add column last_seen_at timestamptz not null default now(),
  add column active       boolean     not null default true,
  add column fetch_tier   text,
  add column fetch_error  text;

-- The old location_text column is redundant once locations[] is in place.
alter table public.jobs drop column location_text;

create index jobs_last_seen_idx on public.jobs (last_seen_at desc);
create index jobs_active_idx    on public.jobs (active) where active = true;
