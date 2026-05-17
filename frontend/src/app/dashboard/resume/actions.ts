"use server";

import { revalidatePath } from "next/cache";
import { createClient } from "@/lib/supabase/server";
import { devFlag, isDevBypass } from "@/lib/devflags";
import { extractResume } from "@/lib/extract-resume";

const MAX_PDF_BYTES = 8 * 1024 * 1024;
const STORAGE_BUCKET = "resumes";

export async function replaceResume(formData: FormData) {
  const file = formData.get("pdf_file");
  if (!(file instanceof File) || file.size === 0) {
    return { error: "Choose a PDF file." };
  }
  if (file.size > MAX_PDF_BYTES) {
    return { error: `PDF too large — keep it under ${MAX_PDF_BYTES / 1024 / 1024}MB.` };
  }
  if (file.type && !file.type.includes("pdf")) {
    return { error: "That doesn't look like a PDF." };
  }
  const bytes = Buffer.from(await file.arrayBuffer());
  const pdfBase64 = bytes.toString("base64");

  if (!devFlag("runExtraction")) {
    return { error: "Extraction is off in dev. Flip runExtraction in devflags.ts." };
  }
  const extracted = await extractResume({ pdfBase64 });
  if (!extracted.ok) return { error: `extraction failed: ${extracted.error}` };

  if (isDevBypass()) {
    revalidatePath("/dashboard/resume");
    return { ok: true as const };
  }

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) return { error: "not signed in" };

  const path = `${user.id}/master.pdf`;
  const { error: uploadErr } = await supabase.storage
    .from(STORAGE_BUCKET)
    .upload(path, bytes, {
      contentType: "application/pdf",
      upsert: true,
    });
  if (uploadErr) return { error: `storage upload failed: ${uploadErr.message}` };

  const { error: resumeErr } = await supabase
    .from("user_resumes")
    .upsert(
      {
        user_id: user.id,
        resume_text: extracted.resume.text,
        master_pdf_storage_path: path,
      },
      { onConflict: "user_id" },
    );
  if (resumeErr) return { error: resumeErr.message };

  revalidatePath("/dashboard/resume");
  return { ok: true as const };
}
