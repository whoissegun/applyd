-- Revert the pg_cron sweep. Layers 1-3 (ATS-list miss, apply 404, enrichment
-- 404) are sufficient. JobsRepo.sweep_stale() stays available in Python for
-- ad-hoc / manual sweeps if needed.
--
-- pg_cron extension is left enabled (harmless, no scheduled jobs).

do $$
begin
  perform cron.unschedule('applyd-sweep-stale-jobs');
exception when others then
  null;  -- already unscheduled
end $$;

drop function if exists internal.sweep_stale_jobs(interval);
