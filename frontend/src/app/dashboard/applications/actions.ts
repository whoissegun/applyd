"use server";

import { createClient } from "@/lib/supabase/server";

const STORAGE_BUCKET = "resumes";
const SIGNED_URL_TTL = 60 * 10; // 10 minutes — enough to open/download

type ResumeUrlResult = { url?: string; error?: string };

/**
 * Signed URL to the exact tailored resume an application was submitted with.
 *
 * Resolves the application's bound `tailored_resume_id` (falls back to the
 * newest version for the job, for pre-versioning rows), then signs the private
 * `resumes` object. Everything runs through the user's RLS-scoped session, so a
 * user can only ever reach their own resumes.
 */
export async function getApplicationResumeUrl(
  applicationId: string,
): Promise<ResumeUrlResult> {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) return { error: "Not signed in." };

  const { data: app } = await supabase
    .from("applications")
    .select("id, tailored_resume_id, job_id")
    .eq("id", applicationId)
    .maybeSingle();
  if (!app) return { error: "Application not found." };

  let path: string | null = null;
  if (app.tailored_resume_id) {
    const { data: tr } = await supabase
      .from("tailored_resumes")
      .select("pdf_storage_path")
      .eq("id", app.tailored_resume_id)
      .maybeSingle();
    path = tr?.pdf_storage_path ?? null;
  }
  if (!path && app.job_id) {
    const { data: tr } = await supabase
      .from("tailored_resumes")
      .select("pdf_storage_path")
      .eq("user_id", user.id)
      .eq("job_id", app.job_id)
      .order("generated_at", { ascending: false })
      .limit(1)
      .maybeSingle();
    path = tr?.pdf_storage_path ?? null;
  }
  if (!path) return { error: "No tailored resume for this application yet." };

  const { data: signed, error } = await supabase.storage
    .from(STORAGE_BUCKET)
    .createSignedUrl(path, SIGNED_URL_TTL);
  if (error || !signed?.signedUrl) {
    return { error: error?.message ?? "Couldn't generate a resume link." };
  }
  return { url: signed.signedUrl };
}
