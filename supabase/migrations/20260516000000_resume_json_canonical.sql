-- Make Resume JSON the canonical resume form.
--
-- `latex_source` becomes one of several input formats (alongside pasted text
-- and uploaded PDFs); `resume_json` is what the tailor + render layers consume.
-- `latex_source` is kept (nullable) for power users who pasted Jake's LaTeX,
-- but tailor reads exclusively from `resume_json`.

alter table public.user_resumes
  add column if not exists resume_json   jsonb,
  add column if not exists source_format text;

alter table public.user_resumes
  alter column latex_source drop not null;

alter table public.tailored_resumes
  add column if not exists resume_json jsonb;
