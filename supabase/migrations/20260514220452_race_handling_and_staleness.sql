-- Race handling + staleness instrumentation.
--
-- 1. 'in_progress' application status — workers claim work via
--    UPDATE ... WHERE status IN (...) RETURNING * (atomic optimistic lock).
-- 2. Unique (user_id, job_id) on tailored_resumes — one tailored output per
--    (user, job). Forces serialization of parallel tailor attempts.
-- 3. Diagnostic columns on jobs: when/why we marked it inactive.

-- 1. Application status: add 'in_progress'
alter table public.applications drop constraint applications_status_check;
alter table public.applications add constraint applications_status_check
  check (status in ('pending', 'tailored', 'in_progress', 'applied', 'skipped', 'failed'));

-- 2. One tailored resume per (user, job)
create unique index tailored_resumes_user_job_uniq
  on public.tailored_resumes (user_id, job_id);

-- 3. Staleness diagnostics on jobs
alter table public.jobs
  add column inactive_reason    text,
  add column marked_inactive_at timestamptz;

-- Helpful for the nightly sweep query
create index jobs_active_last_seen_idx
  on public.jobs (active, last_seen_at)
  where active = true;
