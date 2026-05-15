-- Nightly staleness sweep via pg_cron.
--
-- Layers 1-3 of the stale-detection plan (ATS-list miss, apply 404,
-- enrichment 404) fire from application code on the same execution. This
-- migration adds layer 4: the safety net for jobs that don't get touched
-- by any of those paths.

create extension if not exists pg_cron with schema extensions;

-- Locked to internal schema per Supabase security guidance for SECURITY DEFINER.
create or replace function internal.sweep_stale_jobs(stale_after interval default '30 days')
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  swept integer;
begin
  with updated as (
    update public.jobs
    set active             = false,
        inactive_reason    = 'stale_' || floor(extract(epoch from stale_after) / 86400)::text || 'd',
        marked_inactive_at = now()
    where active = true
      and last_seen_at < now() - stale_after
    returning 1
  )
  select count(*)::int into swept from updated;
  return swept;
end;
$$;

revoke all on function internal.sweep_stale_jobs(interval) from public;

-- Unschedule first (idempotent re-run of this migration), then schedule.
-- pg_cron lives in the cron schema; jobs are identified by name.
do $$
begin
  perform cron.unschedule('applyd-sweep-stale-jobs');
exception when others then
  null;  -- not previously scheduled — fine
end $$;

select cron.schedule(
  'applyd-sweep-stale-jobs',
  '17 3 * * *',  -- 03:17 UTC daily (offset from common cron-collision o'clock)
  $sweep$select internal.sweep_stale_jobs()$sweep$
);
