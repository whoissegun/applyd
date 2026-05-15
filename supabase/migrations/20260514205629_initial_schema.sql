-- applyd initial schema: multi-tenant SaaS
--
-- Layout:
--   public.*    Tenant-facing tables, exposed via Data API
--   internal.*  Worker-only caches, never exposed
--
-- Conventions:
--   - All public tables enable RLS.
--   - Owner-only tables filter by user_id = auth.uid().
--   - Shared catalog (jobs, companies) is SELECT-to-authenticated; writes via service_role.
--   - Explicit GRANTs because new Supabase projects no longer auto-expose tables (breaking change 2026-04-28).
--   - Storage policies live in a separate migration (created after buckets exist).

----------------------------------------------------------------------
-- Schemas + extensions
----------------------------------------------------------------------

create schema if not exists internal;
revoke all on schema internal from anon, authenticated;

create extension if not exists pgcrypto with schema extensions;

----------------------------------------------------------------------
-- Updated-at trigger helper
----------------------------------------------------------------------

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

----------------------------------------------------------------------
-- user_profiles
----------------------------------------------------------------------

create table public.user_profiles (
  id                          uuid        primary key references auth.users(id) on delete cascade,
  full_name                   text        not null,
  phone                       text,
  linkedin_url                text,
  github_url                  text,
  portfolio_url               text,
  work_auth_summary           text,
  sponsorship_needed_countries text[]     not null default '{}',
  target_levels               text[]      not null default '{}',
  target_specialties          text[]      not null default '{}',
  target_locations            jsonb       not null default '{}'::jsonb,
  strategy                    text        not null default 'volume'
                                          check (strategy in ('volume', 'precision')),
  created_at                  timestamptz not null default now(),
  updated_at                  timestamptz not null default now()
);

create trigger user_profiles_set_updated_at
  before update on public.user_profiles
  for each row execute function public.set_updated_at();

alter table public.user_profiles enable row level security;

create policy "user_profiles owner select" on public.user_profiles
  for select to authenticated using (id = auth.uid());
create policy "user_profiles owner insert" on public.user_profiles
  for insert to authenticated with check (id = auth.uid());
create policy "user_profiles owner update" on public.user_profiles
  for update to authenticated using (id = auth.uid()) with check (id = auth.uid());
create policy "user_profiles owner delete" on public.user_profiles
  for delete to authenticated using (id = auth.uid());

grant select, insert, update, delete on public.user_profiles to authenticated;

----------------------------------------------------------------------
-- user_resumes (single active master per user, content inline)
----------------------------------------------------------------------

create table public.user_resumes (
  id            uuid        primary key default gen_random_uuid(),
  user_id       uuid        not null references auth.users(id) on delete cascade,
  latex_source  text        not null,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- One active master per user; if we add variants later, drop this and add an is_active flag.
create unique index user_resumes_one_per_user on public.user_resumes (user_id);

create trigger user_resumes_set_updated_at
  before update on public.user_resumes
  for each row execute function public.set_updated_at();

alter table public.user_resumes enable row level security;

create policy "user_resumes owner select" on public.user_resumes
  for select to authenticated using (user_id = auth.uid());
create policy "user_resumes owner insert" on public.user_resumes
  for insert to authenticated with check (user_id = auth.uid());
create policy "user_resumes owner update" on public.user_resumes
  for update to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy "user_resumes owner delete" on public.user_resumes
  for delete to authenticated using (user_id = auth.uid());

grant select, insert, update, delete on public.user_resumes to authenticated;

----------------------------------------------------------------------
-- user_subscriptions (Stripe state; written by service_role only)
----------------------------------------------------------------------

create table public.user_subscriptions (
  user_id                 uuid        primary key references auth.users(id) on delete cascade,
  stripe_customer_id      text,
  stripe_subscription_id  text,
  tier                    text        not null default 'free'
                                      check (tier in ('free', 'pro', 'unlimited')),
  status                  text        not null default 'active',
  current_period_end      timestamptz,
  monthly_budget_cents    integer     not null default 0,
  created_at              timestamptz not null default now(),
  updated_at              timestamptz not null default now()
);

create trigger user_subscriptions_set_updated_at
  before update on public.user_subscriptions
  for each row execute function public.set_updated_at();

alter table public.user_subscriptions enable row level security;

-- Read-only for owners; writes via service_role (Stripe webhooks).
create policy "user_subscriptions owner select" on public.user_subscriptions
  for select to authenticated using (user_id = auth.uid());

grant select on public.user_subscriptions to authenticated;

----------------------------------------------------------------------
-- companies (shared catalog)
----------------------------------------------------------------------

create table public.companies (
  id                uuid        primary key default gen_random_uuid(),
  canonical_name    text        not null,
  ats               text,
  ats_slug          text,
  careers_url       text,
  first_seen_at     timestamptz not null default now(),
  last_resolved_at  timestamptz
);

create unique index companies_ats_slug_uniq on public.companies (ats, ats_slug) where ats is not null;
create unique index companies_canonical_name_uniq on public.companies (lower(canonical_name));

alter table public.companies enable row level security;

create policy "companies read all authenticated" on public.companies
  for select to authenticated using (true);

grant select on public.companies to authenticated;

----------------------------------------------------------------------
-- jobs (shared catalog; the global pool)
----------------------------------------------------------------------

create table public.jobs (
  id              text        primary key,
  company_id      uuid        references public.companies(id) on delete set null,
  ats             text,
  ats_job_id      text,
  url             text        not null,
  apply_url       text,
  apply_gate      text,
  title           text        not null,
  description     text,
  level           text,
  specialty       text,
  location_text   text,
  is_remote       boolean,
  posted_at       timestamptz,
  discovered_at   timestamptz not null default now(),
  enriched_at     timestamptz,
  raw_payload     jsonb
);

create index jobs_company_idx       on public.jobs (company_id);
create index jobs_level_idx         on public.jobs (level)        where level is not null;
create index jobs_specialty_idx     on public.jobs (specialty)    where specialty is not null;
create index jobs_posted_at_idx     on public.jobs (posted_at desc nulls last);
create index jobs_enriched_at_idx   on public.jobs (enriched_at)  where enriched_at is null;

alter table public.jobs enable row level security;

create policy "jobs read all authenticated" on public.jobs
  for select to authenticated using (true);

grant select on public.jobs to authenticated;

----------------------------------------------------------------------
-- applications (per-user lifecycle row)
----------------------------------------------------------------------

create table public.applications (
  id                    uuid        primary key default gen_random_uuid(),
  user_id               uuid        not null references auth.users(id) on delete cascade,
  job_id                text        not null references public.jobs(id) on delete cascade,
  status                text        not null default 'pending'
                                    check (status in ('pending', 'tailored', 'applied', 'skipped', 'failed')),
  reason                text,
  tailored_resume_id    uuid,   -- FK added after tailored_resumes is created
  applied_at            timestamptz,
  attempted_count       integer     not null default 0,
  last_attempt_at       timestamptz,
  last_error            text,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  unique (user_id, job_id)
);

create index applications_user_status_idx   on public.applications (user_id, status);
create index applications_user_applied_idx  on public.applications (user_id, applied_at desc) where applied_at is not null;

create trigger applications_set_updated_at
  before update on public.applications
  for each row execute function public.set_updated_at();

alter table public.applications enable row level security;

create policy "applications owner select" on public.applications
  for select to authenticated using (user_id = auth.uid());
create policy "applications owner insert" on public.applications
  for insert to authenticated with check (user_id = auth.uid());
create policy "applications owner update" on public.applications
  for update to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy "applications owner delete" on public.applications
  for delete to authenticated using (user_id = auth.uid());

grant select, insert, update, delete on public.applications to authenticated;

----------------------------------------------------------------------
-- tailored_resumes
----------------------------------------------------------------------

create table public.tailored_resumes (
  id                  uuid        primary key default gen_random_uuid(),
  user_id             uuid        not null references auth.users(id) on delete cascade,
  job_id              text        not null references public.jobs(id) on delete cascade,
  source_resume_id    uuid        references public.user_resumes(id) on delete set null,
  latex_source        text        not null,
  pdf_storage_path    text,
  tailor_metadata     jsonb       not null default '{}'::jsonb,
  model_used          text,
  prompt_tokens       integer,
  completion_tokens   integer,
  cached_tokens       integer,
  validator_passed    boolean     not null default false,
  generated_at        timestamptz not null default now()
);

create index tailored_resumes_user_job_idx on public.tailored_resumes (user_id, job_id);

alter table public.tailored_resumes enable row level security;

create policy "tailored_resumes owner select" on public.tailored_resumes
  for select to authenticated using (user_id = auth.uid());
create policy "tailored_resumes owner insert" on public.tailored_resumes
  for insert to authenticated with check (user_id = auth.uid());
create policy "tailored_resumes owner update" on public.tailored_resumes
  for update to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy "tailored_resumes owner delete" on public.tailored_resumes
  for delete to authenticated using (user_id = auth.uid());

grant select, insert, update, delete on public.tailored_resumes to authenticated;

-- Close the circular reference now that tailored_resumes exists.
alter table public.applications
  add constraint applications_tailored_resume_fk
  foreign key (tailored_resume_id) references public.tailored_resumes(id) on delete set null;

----------------------------------------------------------------------
-- apply_attempts (one row per dispatch; transcript only on failure/skip)
----------------------------------------------------------------------

create table public.apply_attempts (
  id                            uuid        primary key default gen_random_uuid(),
  application_id                uuid        not null references public.applications(id) on delete cascade,
  user_id                       uuid        not null references auth.users(id) on delete cascade,
  started_at                    timestamptz not null default now(),
  ended_at                      timestamptz,
  status                        text        not null default 'pending'
                                            check (status in ('pending', 'applied', 'skipped', 'failed')),
  reason                        text,
  agent_transcript_storage_path text,
  prompt_tokens                 integer,
  completion_tokens             integer,
  browser_seconds               integer
);

create index apply_attempts_application_idx on public.apply_attempts (application_id, started_at desc);
create index apply_attempts_user_idx        on public.apply_attempts (user_id, started_at desc);

alter table public.apply_attempts enable row level security;

create policy "apply_attempts owner select" on public.apply_attempts
  for select to authenticated using (user_id = auth.uid());

grant select on public.apply_attempts to authenticated;

----------------------------------------------------------------------
-- usage_events (granular billing line items; writes via service_role)
----------------------------------------------------------------------

create table public.usage_events (
  id          uuid        primary key default gen_random_uuid(),
  user_id     uuid        not null references auth.users(id) on delete cascade,
  event_type  text        not null
              check (event_type in ('tailor', 'apply', 'enrich')),
  cost_cents  integer     not null default 0,
  metadata    jsonb       not null default '{}'::jsonb,
  occurred_at timestamptz not null default now()
);

create index usage_events_user_time_idx on public.usage_events (user_id, occurred_at desc);

alter table public.usage_events enable row level security;

create policy "usage_events owner select" on public.usage_events
  for select to authenticated using (user_id = auth.uid());

grant select on public.usage_events to authenticated;

----------------------------------------------------------------------
-- internal.* (worker-only caches; not exposed to Data API)
----------------------------------------------------------------------

create table internal.resolver_cache (
  company_name  text        primary key,
  ats           text,
  ats_slug      text,
  resolved_at   timestamptz not null default now()
);

create table internal.broad_search_cache (
  cache_key   text        primary key,
  payload     jsonb       not null,
  fetched_at  timestamptz not null default now(),
  ttl_seconds integer     not null
);

-- service_role bypasses RLS and uses postgres role privileges; no grants
-- needed for anon/authenticated. Schema-level revoke above keeps internal.*
-- unreachable to public roles.
