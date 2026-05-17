-- Drop the JSON-canonical attempt. Master resume becomes plain text;
-- the tailor reads it directly + a filled Jake's example for formatting.
-- The original PDF lives in Supabase Storage at users/{uid}/master.pdf.

alter table public.user_resumes drop column if exists resume_json;
alter table public.user_resumes drop column if exists source_format;
alter table public.tailored_resumes drop column if exists resume_json;

alter table public.user_resumes rename column latex_source to resume_text;

alter table public.user_resumes
  add column if not exists master_pdf_storage_path text;
