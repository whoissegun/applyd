"use client";

import { useState, useTransition } from "react";
import { extractUploadedResume, finishOnboarding } from "./actions";
import type { ExtractedResume } from "@/lib/extract-resume";

type Step = "resume" | "basics" | "roles" | "work-auth";

const STEPS: Step[] = ["resume", "basics", "roles", "work-auth"];
const TITLES: Record<Step, { title: string; sub: string }> = {
  resume: { title: "Your resume", sub: "Upload your PDF. We'll pull the rest." },
  basics: { title: "About you", sub: "Pulled from your resume. Edit anything that's wrong." },
  roles: { title: "What you're looking for", sub: "Our best guess from your resume. Tighten or widen it." },
  "work-auth": { title: "Work authorization", sub: "Last thing — how forms get answered." },
};

type Basics = {
  full_name: string;
  phone: string;
  linkedin_url: string;
  github_url: string;
  portfolio_url: string;
};

type WorkAuthEdit = {
  work_auth_summary: string;
  sponsorship_needed_countries: string;
};

const EMPTY_BASICS: Basics = {
  full_name: "",
  phone: "",
  linkedin_url: "",
  github_url: "",
  portfolio_url: "",
};

const EMPTY_WA: WorkAuthEdit = {
  work_auth_summary: "",
  sponsorship_needed_countries: "",
};

export function OnboardingWizard() {
  const [step, setStep] = useState<Step>("resume");
  const [pdfFile, setPdfFile] = useState<File | null>(null);
  const [pdfBase64, setPdfBase64] = useState<string>("");
  const [extracted, setExtracted] = useState<ExtractedResume | null>(null);
  const [basics, setBasics] = useState<Basics>(EMPTY_BASICS);
  const [targetRoles, setTargetRoles] = useState<string>("");
  const [wa, setWa] = useState<WorkAuthEdit>(EMPTY_WA);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  function go(next: Step) {
    setError(null);
    setStep(next);
  }

  async function handleExtract() {
    if (!pdfFile) {
      setError("Choose a PDF file.");
      return;
    }
    setError(null);
    const arrayBuf = await pdfFile.arrayBuffer();
    const b64 = Buffer.from(new Uint8Array(arrayBuf)).toString("base64");
    setPdfBase64(b64);

    const fd = new FormData();
    fd.append("pdf_file", pdfFile);

    startTransition(async () => {
      const out = await extractUploadedResume(fd);
      if (!out.ok) {
        setError(out.error);
        return;
      }
      setExtracted(out.resume);
      setBasics({
        full_name: out.resume.contact.name ?? "",
        phone: out.resume.contact.phone ?? "",
        linkedin_url: out.resume.contact.linkedin_url ?? "",
        github_url: out.resume.contact.github_url ?? "",
        portfolio_url: out.resume.contact.portfolio_url ?? "",
      });
      setTargetRoles(out.resume.targetRoles ?? "");
      setWa({
        work_auth_summary: out.resume.workAuth.summary ?? "",
        sponsorship_needed_countries:
          out.resume.workAuth.sponsorship_needed_countries.join(", "),
      });
      go("basics");
    });
  }

  async function handleFinish() {
    if (!extracted) {
      setError("Upload your resume first.");
      return;
    }
    if (!basics.full_name.trim()) {
      setError("Name is required.");
      return;
    }
    setError(null);
    startTransition(async () => {
      try {
        await finishOnboarding({
          resumeText: extracted.text,
          pdfBase64,
          contact: extracted.contact,
          workAuth: extracted.workAuth,
          edited: {
            ...basics,
            target_roles: targetRoles,
            ...wa,
          },
        });
      } catch (e) {
        setError((e as Error).message);
      }
    });
  }

  const meta = TITLES[step];
  const stepIdx = STEPS.indexOf(step) + 1;

  return (
    <div className="flex flex-col gap-8">
      <div className="flex items-center gap-3 mono" style={{ fontSize: 12 }}>
        {STEPS.map((s, i) => (
          <div key={s} className="flex items-center gap-3">
            <span
              className="flex items-center justify-center rounded-full"
              style={{
                width: 22,
                height: 22,
                background: s === step ? "var(--color-ghost)" : "transparent",
                color: s === step ? "var(--color-obsidian)" : "var(--color-pebble)",
                boxShadow: s === step ? "none" : "inset 0 0 0 1px var(--color-hairline-strong)",
                fontWeight: 500,
              }}
            >
              {i + 1}
            </span>
            <span style={{ color: s === step ? "var(--color-ghost)" : "var(--color-pebble)" }}>
              {TITLES[s].title}
            </span>
            {i < STEPS.length - 1 ? (
              <span style={{ width: 24, height: 1, background: "var(--color-hairline)" }} />
            ) : null}
          </div>
        ))}
      </div>

      <div>
        <h1 className="h-section">{meta.title}</h1>
        <p className="mt-2" style={{ color: "var(--color-pebble)", fontSize: "var(--text-sm)" }}>
          Step {stepIdx} of {STEPS.length} · {meta.sub}
        </p>
      </div>

      {step === "resume" && (
        <ResumeStep
          file={pdfFile}
          setFile={setPdfFile}
          onContinue={handleExtract}
          pending={pending}
          error={error}
        />
      )}

      {step === "basics" && (
        <BasicsStep
          value={basics}
          onChange={setBasics}
          onBack={() => go("resume")}
          onContinue={() => go("roles")}
          error={error}
        />
      )}

      {step === "roles" && (
        <RolesStep
          value={targetRoles}
          onChange={setTargetRoles}
          onBack={() => go("basics")}
          onContinue={() => go("work-auth")}
          error={error}
        />
      )}

      {step === "work-auth" && (
        <WorkAuthStep
          value={wa}
          onChange={setWa}
          onBack={() => go("roles")}
          onFinish={handleFinish}
          pending={pending}
          error={error}
        />
      )}
    </div>
  );
}

/* ────────────────────────────────────────────────────────── */

function ResumeStep(props: {
  file: File | null;
  setFile: (f: File | null) => void;
  onContinue: () => void;
  pending: boolean;
  error: string | null;
}) {
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-1.5">
        <p style={{ color: "#000", fontSize: 14, lineHeight: 1.5 }}>
          Upload your <strong>master resume</strong>. Include everything you&apos;ve ever done.
        </p>
        <p style={{ color: "#777169", fontSize: 12, lineHeight: 1.5 }}>
          For each job we apply to, we strip what isn&apos;t relevant.
        </p>
      </div>

      {props.pending ? (
        <div
          className="flex flex-col items-center justify-center gap-3"
          style={{
            minHeight: 220,
            border: "1px solid var(--color-hairline-strong)",
            borderRadius: 12,
            padding: 24,
            background: "var(--color-whisper-strong)",
          }}
        >
          <Spinner />
          <span style={{ fontSize: 14, color: "#000" }}>Reading your resume…</span>
          {props.file ? (
            <span className="mono" style={{ fontSize: 11, color: "var(--color-pebble)" }}>
              {props.file.name}
            </span>
          ) : null}
          <span className="mono" style={{ fontSize: 11, color: "var(--color-pebble)" }}>
            this takes 10–20 seconds
          </span>
        </div>
      ) : (
        <label
          className="flex items-center justify-center cursor-pointer transition"
          style={{
            minHeight: 220,
            border: props.file
              ? "1px solid var(--color-hairline-strong)"
              : "1px dashed var(--color-hairline-strong)",
            borderRadius: 12,
            padding: 24,
            background: props.file ? "var(--color-whisper-strong)" : "transparent",
          }}
        >
          <input
            type="file"
            accept="application/pdf,.pdf"
            className="hidden"
            onChange={(e) => props.setFile(e.target.files?.[0] ?? null)}
          />
          {props.file ? (
            <div className="flex flex-col items-center gap-2">
              <span className="mono" style={{ fontSize: 13, color: "#000" }}>
                {props.file.name}
              </span>
              <span className="mono" style={{ fontSize: 11, color: "var(--color-pebble)" }}>
                {(props.file.size / 1024).toFixed(0)} KB · click to replace
              </span>
            </div>
          ) : (
            <div className="flex flex-col items-center gap-2">
              <span style={{ fontSize: 14, color: "#000" }}>Click to choose a PDF</span>
              <span className="mono" style={{ fontSize: 11, color: "var(--color-pebble)" }}>
                max 8 MB
              </span>
            </div>
          )}
        </label>
      )}

      <p style={{ color: "#777169", fontSize: 12, lineHeight: 1.5 }}>
        When we apply to jobs for you, we rewrite your resume in{" "}
        <a
          href="https://github.com/jakegut/resume"
          target="_blank"
          rel="noopener noreferrer"
          className="underline underline-offset-2"
          style={{ color: "#000" }}
        >
          this template
        </a>{" "}
        built for ATS.
      </p>

      {props.error ? <p style={{ color: "#ff4704", fontSize: 13 }}>{props.error}</p> : null}

      <div className="flex items-center justify-end pt-2">
        <button
          type="button"
          disabled={props.pending || !props.file}
          onClick={props.onContinue}
          className="pill pill-primary"
        >
          {props.pending ? "Reading…" : "Continue →"}
        </button>
      </div>
    </div>
  );
}

function Spinner() {
  return (
    <span
      aria-hidden
      style={{
        width: 22,
        height: 22,
        borderRadius: "50%",
        border: "2px solid var(--color-hairline-strong)",
        borderTopColor: "#000",
        display: "inline-block",
        animation: "applyd-spin 0.7s linear infinite",
      }}
    />
  );
}

function BasicsStep(props: {
  value: Basics;
  onChange: (b: Basics) => void;
  onBack: () => void;
  onContinue: () => void;
  error: string | null;
}) {
  const { value, onChange } = props;
  const set = <K extends keyof Basics>(k: K, v: string) =>
    onChange({ ...value, [k]: v });
  return (
    <div className="card flex flex-col gap-5">
      <p style={{ color: "#777169", fontSize: 13, lineHeight: 1.55 }}>
        We pulled these from your resume. Anything wrong? Edit it.
      </p>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="md:col-span-2">
          <label className="label">Full legal name *</label>
          <input
            value={value.full_name}
            onChange={(e) => set("full_name", e.target.value)}
            required
            className="input"
            placeholder="Jane Example"
          />
        </div>
        <div>
          <label className="label">Phone</label>
          <input
            value={value.phone}
            onChange={(e) => set("phone", e.target.value)}
            className="input"
            placeholder="+1 555 555 0123"
          />
        </div>
        <div>
          <label className="label">LinkedIn URL</label>
          <input
            value={value.linkedin_url}
            onChange={(e) => set("linkedin_url", e.target.value)}
            className="input"
            placeholder="https://linkedin.com/in/…"
          />
        </div>
        <div>
          <label className="label">GitHub URL</label>
          <input
            value={value.github_url}
            onChange={(e) => set("github_url", e.target.value)}
            className="input"
            placeholder="https://github.com/…"
          />
        </div>
        <div>
          <label className="label">Portfolio URL</label>
          <input
            value={value.portfolio_url}
            onChange={(e) => set("portfolio_url", e.target.value)}
            className="input"
            placeholder="https://…"
          />
        </div>
      </div>

      {props.error ? <p style={{ color: "#ff4704", fontSize: 13 }}>{props.error}</p> : null}

      <div className="flex items-center justify-between pt-2">
        <button type="button" onClick={props.onBack} className="pill pill-ghost pill-sm">
          ← Back
        </button>
        <button type="button" onClick={props.onContinue} className="pill pill-primary">
          Continue →
        </button>
      </div>
    </div>
  );
}

function RolesStep(props: {
  value: string;
  onChange: (v: string) => void;
  onBack: () => void;
  onContinue: () => void;
  error: string | null;
}) {
  return (
    <div className="card flex flex-col gap-5">
      <p style={{ color: "#777169", fontSize: 13, lineHeight: 1.55 }}>
        Our guess based on your resume. Tighten it (specific specialty), widen it
        (multiple disciplines), or rewrite entirely.
      </p>

      <div>
        <label className="label">Target roles</label>
        <textarea
          value={props.value}
          onChange={(e) => props.onChange(e.target.value)}
          className="input input-textarea"
          rows={10}
          style={{ minHeight: 240, lineHeight: 1.5 }}
          placeholder="The job titles, seniority, and industries you're targeting. The more specific the better."
        />
        <p className="mt-2" style={{ color: "#777169", fontSize: 12 }}>
          The matcher uses this to filter jobs we send to the tailor.
        </p>
      </div>

      {props.error ? <p style={{ color: "#ff4704", fontSize: 13 }}>{props.error}</p> : null}

      <div className="flex items-center justify-between pt-2">
        <button type="button" onClick={props.onBack} className="pill pill-ghost pill-sm">
          ← Back
        </button>
        <button type="button" onClick={props.onContinue} className="pill pill-primary">
          Continue →
        </button>
      </div>
    </div>
  );
}

function WorkAuthStep(props: {
  value: WorkAuthEdit;
  onChange: (w: WorkAuthEdit) => void;
  onBack: () => void;
  onFinish: () => void;
  pending: boolean;
  error: string | null;
}) {
  const { value, onChange } = props;
  const set = <K extends keyof WorkAuthEdit>(k: K, v: string) =>
    onChange({ ...value, [k]: v });
  return (
    <div className="card flex flex-col gap-5">
      <p style={{ color: "#777169", fontSize: 13, lineHeight: 1.55 }}>
        We pulled what we could from your resume. Confirm or fix.
      </p>

      <div>
        <label className="label">How should we describe your situation on forms?</label>
        <textarea
          value={value.work_auth_summary}
          onChange={(e) => set("work_auth_summary", e.target.value)}
          className="input input-textarea"
          rows={4}
          placeholder="e.g. Authorized to work in Canada. Need sponsorship for the US, EU, UK."
        />
        <p className="mt-2" style={{ color: "#777169", fontSize: 12 }}>
          Applications will quote this when asked about your work eligibility.
        </p>
      </div>

      <div>
        <label className="label">Countries that need sponsorship (comma-separated)</label>
        <input
          value={value.sponsorship_needed_countries}
          onChange={(e) => set("sponsorship_needed_countries", e.target.value)}
          className="input"
          placeholder="US, UK, Germany"
        />
      </div>

      {props.error ? <p style={{ color: "#ff4704", fontSize: 13 }}>{props.error}</p> : null}

      <div className="flex items-center justify-between pt-2">
        <button type="button" onClick={props.onBack} className="pill pill-ghost pill-sm">
          ← Back
        </button>
        <button
          type="button"
          disabled={props.pending}
          onClick={props.onFinish}
          className="pill pill-primary"
        >
          {props.pending ? "Finishing…" : "Finish setup →"}
        </button>
      </div>
    </div>
  );
}
