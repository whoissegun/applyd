-- Fix: rank_jobs_for_user dies with a statement timeout (57014) at ~16k jobs.
--
-- The original SQL function ordered by `j.embedding <=> p.embedding` where
-- p.embedding came from a cross-joined subquery. An HNSW index scan is only
-- eligible when the ORDER BY compares against a constant/parameter, so the
-- planner fell back to detoasting + scoring every embedded row (~14k × 6KB
-- vectors) and blew Supabase's 8s API statement timeout. The matchmaker has
-- been returning matched=0 on every sweep since the catalog outgrew the scan.
--
-- Rewritten as plpgsql: fetch the profile embedding into a variable first,
-- then ORDER BY against that parameter — the existing jobs_embedding_idx
-- (hnsw, vector_cosine_ops) now serves the top-k directly.

create or replace function public.rank_jobs_for_user(p_user_id uuid, p_limit int default 50)
returns table (id text, title text, classification jsonb, distance double precision)
language plpgsql
stable
set hnsw.ef_search = 200  -- headroom: WHERE clauses post-filter the index stream
as $$
declare
  v_emb extensions.vector(1536);
begin
  select embedding into v_emb from public.user_profiles where user_profiles.id = p_user_id;
  if v_emb is null then
    return;
  end if;
  return query
  select
    j.id,
    j.title,
    j.classification,
    (j.embedding operator(extensions.<=>) v_emb)::double precision as distance
  from public.jobs j
  where j.active
    and j.apply_gate is null
    and j.classification is not null
    and j.embedding is not null
    and not exists (
      select 1 from public.applications a
      where a.user_id = p_user_id and a.job_id = j.id
    )
  order by j.embedding operator(extensions.<=>) v_emb
  limit p_limit;
end
$$;

-- pgvector >= 0.8: iterative index scans keep pulling from the index when
-- post-filtering (the not-exists, gates) discards candidates, instead of
-- under-returning. Guarded so the migration also applies on older pgvector.
do $$
begin
  if exists (
    select 1 from pg_extension
    where extname = 'vector' and extversion >= '0.8.0'
  ) then
    execute $sql$
      alter function public.rank_jobs_for_user(uuid, int)
        set hnsw.iterative_scan = 'relaxed_order'
    $sql$;
  end if;
end
$$;

-- CREATE OR REPLACE preserves the original ACL (execute revoked from
-- public/anon/authenticated in 20260610000000); re-assert anyway for safety.
revoke execute on function public.rank_jobs_for_user(uuid, int) from public, anon, authenticated;
