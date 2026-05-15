-- Single private bucket for all per-user PDF blobs.
--
-- Path scheme:
--   {user_id}/master.pdf                 → compiled master resume (optional cache)
--   {user_id}/tailored/{job_id}.pdf      → tailored resume per (user, job)
--
-- The bucket is private. Per-user access is enforced by RLS on storage.objects:
-- a row is owned by `auth.uid()` iff the first folder segment of the object name
-- equals the user's id.
--
-- IMPORTANT: upsert needs INSERT + SELECT + UPDATE policies, not just INSERT.
-- A missing SELECT or UPDATE policy makes upserts silently fail.

insert into storage.buckets (id, name, public)
values ('resumes', 'resumes', false)
on conflict (id) do nothing;

-- Owner = the user whose id matches the first segment of the object path.
-- Using `storage.foldername(name)` (returns a text[] split on '/') keeps the
-- policy resilient to deeper trees like {uid}/tailored/{job_id}.pdf.

create policy "resumes owner select"
  on storage.objects for select to authenticated
  using (
    bucket_id = 'resumes'
    and (storage.foldername(name))[1] = auth.uid()::text
  );

create policy "resumes owner insert"
  on storage.objects for insert to authenticated
  with check (
    bucket_id = 'resumes'
    and (storage.foldername(name))[1] = auth.uid()::text
  );

create policy "resumes owner update"
  on storage.objects for update to authenticated
  using (
    bucket_id = 'resumes'
    and (storage.foldername(name))[1] = auth.uid()::text
  )
  with check (
    bucket_id = 'resumes'
    and (storage.foldername(name))[1] = auth.uid()::text
  );

create policy "resumes owner delete"
  on storage.objects for delete to authenticated
  using (
    bucket_id = 'resumes'
    and (storage.foldername(name))[1] = auth.uid()::text
  );
