-- Company dedup strategy (cheapest → most accurate, all four layered):
--
--   1. Manual aliases (public.company_aliases) — for the long tail / renames
--   2. ATS slug overlap — two name variants both resolving to greenhouse:stripe = same company
--   3. Domain match (companies.primary_domain) — stripe.com is one truth across name variants
--   4. Normalized name match (companies.name_normalized) — strips Inc/LLC/etc + punctuation, lowercases
--
-- The normalization function lives in internal so it doesn't leak via the Data API.
-- IMMUTABLE so it can back a generated column. Keep in sync with the Python copy
-- in src/applyd/db/companies_repo.py (_normalize_name).

create or replace function internal.normalize_company_name(input text)
returns text
language sql
immutable
set search_path = ''
as $$
  select trim(
    regexp_replace(
      regexp_replace(
        regexp_replace(
          lower(coalesce(trim(input), '')),
          '\s+(inc|incorporated|llc|ltd|limited|corp|corporation|company|gmbh|s\.a\.|sa|ag|n\.v\.|nv|b\.v\.|bv|plc|kk)\.?$',
          '', 'i'
        ),
        '[^a-z0-9\s]', '', 'g'
      ),
      '\s+', ' ', 'g'
    )
  );
$$;

alter table public.companies
  add column name_normalized text
    generated always as (internal.normalize_company_name(canonical_name)) stored,
  add column primary_domain text;

-- Partial unique indexes so blank normalized strings (shouldn't happen, but
-- defensively) don't collide and missing domains don't either.
create unique index companies_name_normalized_uniq
  on public.companies (name_normalized)
  where name_normalized is not null and name_normalized <> '';

create unique index companies_primary_domain_uniq
  on public.companies (primary_domain)
  where primary_domain is not null;

-- The alias table is server-side dedup state. Authenticated users have no
-- reason to read or write it; service_role bypasses RLS automatically.
create table public.company_aliases (
  alias_normalized text        primary key,
  company_id       uuid        not null references public.companies(id) on delete cascade,
  created_at       timestamptz not null default now()
);

alter table public.company_aliases enable row level security;
-- No policies on purpose — locked to service_role.
