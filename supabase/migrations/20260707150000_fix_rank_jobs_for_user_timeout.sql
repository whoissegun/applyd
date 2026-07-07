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

-- Volatile (not stable) because it sets GUCs via set_config; Postgres forbids
-- SET in non-volatile functions. Function-level `SET hnsw.ef_search` clauses
-- are not an option on Supabase: pgvector's GUCs only register once its
-- library loads in the session, and setting a still-placeholder parameter
-- needs superuser, which the migration role isn't.
create or replace function public.rank_jobs_for_user(p_user_id uuid, p_limit int default 50)
returns table (id text, title text, classification jsonb, distance double precision)
language plpgsql
volatile
as $$
declare
  v_emb extensions.vector(1536);
begin
  select embedding into v_emb from public.user_profiles where user_profiles.id = p_user_id;
  if v_emb is null then
    return;
  end if;
  -- Reading the vector column above loaded pgvector, so its GUCs now exist.
  -- ef_search headroom: the WHERE clauses post-filter the index stream, and
  -- the default (40) is below p_limit anyway. Transaction-local.
  perform set_config('hnsw.ef_search', '200', true);
  begin
    -- pgvector >= 0.8: keep pulling from the index when post-filtering
    -- discards candidates, instead of under-returning.
    perform set_config('hnsw.iterative_scan', 'relaxed_order', true);
  exception when others then
    null;  -- older pgvector: GUC doesn't exist; plain scan is fine
  end;
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

-- CREATE OR REPLACE preserves the original ACL (execute revoked from
-- public/anon/authenticated in 20260610000000); re-assert anyway for safety.
revoke execute on function public.rank_jobs_for_user(uuid, int) from public, anon, authenticated;
