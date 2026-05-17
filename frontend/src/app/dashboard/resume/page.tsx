import { createClient } from "@/lib/supabase/server";
import { isDevBypass, MOCK_RESUME } from "@/lib/devflags";
import { ResumeForm } from "./form";

const STORAGE_BUCKET = "resumes";
const SIGNED_URL_TTL = 60 * 10; // 10 minutes — enough to click "Download"

type MasterRow = {
  resume_text: string | null;
  master_pdf_storage_path: string | null;
  updated_at: string | null;
};

export default async function ResumePage() {
  let master: MasterRow | null;
  let pdfDownloadUrl: string | null = null;

  if (isDevBypass()) {
    master = MOCK_RESUME;
  } else {
    const supabase = await createClient();
    const {
      data: { user },
    } = await supabase.auth.getUser();
    const { data } = await supabase
      .from("user_resumes")
      .select("resume_text, master_pdf_storage_path, updated_at")
      .eq("user_id", user!.id)
      .maybeSingle();
    master = (data ?? null) as MasterRow | null;

    if (master?.master_pdf_storage_path) {
      const { data: signed } = await supabase.storage
        .from(STORAGE_BUCKET)
        .createSignedUrl(master.master_pdf_storage_path, SIGNED_URL_TTL);
      pdfDownloadUrl = signed?.signedUrl ?? null;
    }
  }

  const pdfFilename = master?.master_pdf_storage_path
    ? master.master_pdf_storage_path.split("/").slice(-1)[0]
    : null;

  return (
    <div className="px-8 py-8 max-w-[980px] mx-auto flex flex-col gap-8">
      <header>
        <p className="eyebrow">Settings</p>
        <h1 className="h-eleven mt-3">Your resume.</h1>
        <p className="mt-3" style={{ color: "#777169", fontSize: 15, lineHeight: 1.5 }}>
          Your master resume. We pull from it for every tailored application —
          never send it as-is.
        </p>
      </header>
      <ResumeForm
        pdfFilename={pdfFilename}
        pdfDownloadUrl={pdfDownloadUrl}
        updatedAt={master?.updated_at ?? null}
      />
    </div>
  );
}
