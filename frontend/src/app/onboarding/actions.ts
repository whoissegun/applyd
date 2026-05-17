"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { createClient } from "@/lib/supabase/server";
import { devFlag, isDevBypass } from "@/lib/devflags";
import { extractResume, type ExtractedResume } from "@/lib/extract-resume";

const MAX_PDF_BYTES = 8 * 1024 * 1024;
const STORAGE_BUCKET = "resumes";

export type ExtractOutcome =
  | { ok: true; resume: ExtractedResume }
  | { ok: false; error: string };

/** Step 1 — server-side: turn the uploaded PDF into {text, contact, workAuth}.
 *  Does NOT persist yet. The user reviews/edits on the same page before saving.
 */
export async function extractUploadedResume(
  formData: FormData,
): Promise<ExtractOutcome> {
  const file = formData.get("pdf_file");
  if (!(file instanceof File) || file.size === 0) {
    return { ok: false, error: "Choose a PDF file." };
  }
  if (file.size > MAX_PDF_BYTES) {
    return { ok: false, error: `PDF too large — keep it under ${MAX_PDF_BYTES / 1024 / 1024}MB.` };
  }
  if (file.type && !file.type.includes("pdf")) {
    return { ok: false, error: "That doesn't look like a PDF." };
  }
  const pdfBase64 = Buffer.from(await file.arrayBuffer()).toString("base64");

  if (!devFlag("runExtraction")) {
    // Extraction disabled in dev — return a tiny fixture so the wizard works.
    return {
      ok: true,
      resume: {
        text: "(extraction disabled — runExtraction flag is off)",
        contact: {
          name: null,
          phone: null,
          email: null,
          linkedin_url: null,
          github_url: null,
          portfolio_url: null,
        },
        workAuth: { summary: null, sponsorship_needed_countries: [] },
        targetRoles: null,
      },
    };
  }

  const result = await extractResume({ pdfBase64 });
  if (!result.ok) return { ok: false, error: result.error };
  return { ok: true, resume: result.resume };
}

/** Finish — persist everything to Supabase + Storage. Dev bypass = no-op. */
export async function finishOnboarding(args: {
  resumeText: string;
  pdfBase64: string;
  contact: ExtractedResume["contact"];
  workAuth: ExtractedResume["workAuth"];
  edited: {
    full_name: string;
    phone: string;
    linkedin_url: string;
    github_url: string;
    portfolio_url: string;
    target_roles: string;
    work_auth_summary: string;
    sponsorship_needed_countries: string;
  };
}) {
  if (isDevBypass()) {
    revalidatePath("/dashboard");
    redirect("/dashboard");
  }

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) throw new Error("not signed in");
  const userId = user.id;

  // 1. Upload the master PDF to Storage.
  const pdfBytes = Buffer.from(args.pdfBase64, "base64");
  const path = `${userId}/master.pdf`;
  const { error: uploadErr } = await supabase.storage
    .from(STORAGE_BUCKET)
    .upload(path, pdfBytes, {
      contentType: "application/pdf",
      upsert: true,
    });
  if (uploadErr) {
    throw new Error(`storage upload failed: ${uploadErr.message}`);
  }

  // 2. Upsert user_resumes.
  const { error: resumeErr } = await supabase
    .from("user_resumes")
    .upsert(
      {
        user_id: userId,
        resume_text: args.resumeText,
        master_pdf_storage_path: path,
      },
      { onConflict: "user_id" },
    );
  if (resumeErr) throw new Error(resumeErr.message);

  // 3. Update user_profiles. Contact info stays as typed columns (literal
  // form-fill values). Everything narrative — target roles, work auth,
  // sponsorship — collapses into profile_answers as labeled markdown.
  const narrativeParts: string[] = [];
  const tr = args.edited.target_roles.trim();
  if (tr) narrativeParts.push(`## Target roles\n${tr}`);
  const waBits: string[] = [];
  const was = args.edited.work_auth_summary.trim();
  if (was) waBits.push(was);
  const sponsorship = args.edited.sponsorship_needed_countries
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (sponsorship.length > 0) {
    waBits.push(`Need sponsorship to work in: ${sponsorship.join(", ")}`);
  }
  if (waBits.length > 0) {
    narrativeParts.push(`## Work authorization\n${waBits.join("\n")}`);
  }
  const profileAnswers = narrativeParts.join("\n\n");

  const profilePayload: Record<string, unknown> = {
    full_name: args.edited.full_name.trim(),
    phone: args.edited.phone.trim() || null,
    linkedin_url: args.edited.linkedin_url.trim() || null,
    github_url: args.edited.github_url.trim() || null,
    portfolio_url: args.edited.portfolio_url.trim() || null,
    profile_answers: profileAnswers || null,
  };
  const { error: profileErr } = await supabase
    .from("user_profiles")
    .update(profilePayload)
    .eq("id", userId);
  if (profileErr) throw new Error(profileErr.message);

  revalidatePath("/dashboard");
  redirect("/dashboard");
}
