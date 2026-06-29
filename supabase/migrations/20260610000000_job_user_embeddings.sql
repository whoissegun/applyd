-- Embedding-based candidate ranking for the matchmaker.
--
-- Jobs get a vector of their classification text; users get a vector of
-- resume + LLM-extracted positive targets (never the don't-wants — negation
-- pulls vectors the wrong way). The matchmaker ranks unseen jobs by cosine
-- similarity and runs the Haiku judge only on the top slice, instead of
-- scoring the whole catalog newest-first.

create extension if not exists vector with schema extensions;

alter table public.jobs
  add column if not exists embedding extensions.vector(1536);

alter table public.user_profiles
  add column if not exists embedding extensions.vector(1536),
  -- sha256 of the embedded inputs; lets the matchmaker detect a changed
  -- resume/profile and re-embed without a separate staleness column.
  add column if not exists embedding_source_hash text;

create index if not exists jobs_embedding_idx
  on public.jobs using hnsw (embedding extensions.vector_cosine_ops);

-- Ranked candidates for one user: active, classified, embedded, ungated jobs
-- the user has no applications row for, nearest-first. Worker-only (service
-- role); PostgREST's search_path may not include `extensions`, hence the
-- qualified operator.
create or replace function public.rank_jobs_for_user(p_user_id uuid, p_limit int default 50)
returns table (id text, title text, classification jsonb, distance double precision)
language sql
stable
as $$
  select
    j.id,
    j.title,
    j.classification,
    (j.embedding operator(extensions.<=>) p.embedding)::double precision as distance
  from public.jobs j
  cross join (
    select embedding from public.user_profiles where id = p_user_id
  ) p
  where j.active
    and j.apply_gate is null
    and j.classification is not null
    and j.embedding is not null
    and p.embedding is not null
    and not exists (
      select 1 from public.applications a
      where a.user_id = p_user_id and a.job_id = j.id
    )
  order by j.embedding operator(extensions.<=>) p.embedding
  limit p_limit
$$;

-- Worker-only: revoke from API roles (default grants execute to public).
revoke execute on function public.rank_jobs_for_user(uuid, int) from public, anon, authenticated;
