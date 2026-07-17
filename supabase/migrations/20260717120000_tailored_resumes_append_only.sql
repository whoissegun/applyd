-- Tailored resumes become append-only.
--
-- Before: a UNIQUE (user_id, job_id) index meant re-tailoring the same job
-- overwrote the existing row + PDF. An application's `tailored_resume_id` then
-- resolved to whatever the LATEST re-tailor produced — not the resume actually
-- submitted. Now we keep every version, so "the resume used for this
-- application" is always accurate.
--
-- Ordering note: deploy the app code that uses insert-append BEFORE running
-- this. The old code upsert()s with on_conflict (user_id, job_id), which needs
-- this unique index; the new repo insert()s and only depends on the index being
-- gone. New code tolerates the index still being present (dup -> in-place
-- update) so either order is safe, but code-first avoids any window where the
-- old code's upsert has no constraint to target.

drop index if exists public.tailored_resumes_user_job_uniq;

-- Supports get-latest(user, job) — the newest version by generated_at — which
-- the tailor/apply reuse checks and the API fall back to.
create index if not exists tailored_resumes_user_job_recent_idx
  on public.tailored_resumes (user_id, job_id, generated_at desc);
