-- (1) Freeform agent Q&A bank on user_profiles. UI structures the questions;
--     DB stores raw text. Also drop NOT NULL on full_name so the auto-create
--     trigger below can stub a row before the user completes onboarding.

alter table public.user_profiles
  alter column full_name drop not null,
  add column profile_answers text;

-- (2) Preserve application history if a job row is ever purged.
--     Requires job_id to be nullable.

alter table public.applications drop constraint applications_job_id_fkey;
alter table public.applications alter column job_id drop not null;
alter table public.applications
  add constraint applications_job_id_fkey
  foreign key (job_id) references public.jobs(id) on delete set null;

alter table public.tailored_resumes drop constraint tailored_resumes_job_id_fkey;
alter table public.tailored_resumes alter column job_id drop not null;
alter table public.tailored_resumes
  add constraint tailored_resumes_job_id_fkey
  foreign key (job_id) references public.jobs(id) on delete set null;

-- (3) Auto-provision user_profiles + user_subscriptions on signup.
--     Function lives in internal schema (not exposed) per Supabase security
--     guidance: never put security-definer functions in an exposed schema.
--     SECURITY DEFINER + locked search_path keeps the trigger safe.

create or replace function internal.handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.user_profiles (id, full_name)
  values (new.id, nullif(new.raw_user_meta_data ->> 'full_name', ''));

  insert into public.user_subscriptions (user_id)
  values (new.id);

  return new;
end;
$$;

revoke all on function internal.handle_new_auth_user() from public;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function internal.handle_new_auth_user();
