"use client";

import { useActionState, useState } from "react";
import { replaceResume } from "./actions";

type State = { ok?: true; error?: string } | undefined;

async function action(_prev: State, formData: FormData): Promise<State> {
  return replaceResume(formData);
}

export function ResumeForm({
  pdfFilename,
  pdfDownloadUrl,
  updatedAt,
}: {
  pdfFilename: string | null;
  pdfDownloadUrl: string | null;
  updatedAt: string | null;
}) {
  const [pdfFile, setPdfFile] = useState<File | null>(null);
  const [state, formAction, pending] = useActionState<State, FormData>(action, undefined);

  return (
    <form action={formAction} className="flex flex-col gap-6">
      <div
        className="card-flush flex flex-col"
        style={{
          borderRadius: 12,
          border: "1px solid var(--color-hairline)",
          overflow: "hidden",
        }}
      >
        <div
          className="flex items-center justify-between gap-4 px-4 py-3"
          style={{ borderBottom: "1px solid var(--color-hairline)" }}
        >
          <div className="flex flex-col gap-1">
            <span
              className="mono"
              style={{
                fontSize: 11,
                letterSpacing: "0.04em",
                textTransform: "uppercase",
                color: "var(--color-steel)",
              }}
            >
              Current master resume
            </span>
            <span className="mono" style={{ fontSize: 11, color: "var(--color-pebble)" }}>
              {pdfFilename
                ? updatedAt
                  ? `updated ${new Date(updatedAt).toLocaleString()}`
                  : ""
                : "No resume on file yet."}
            </span>
          </div>
          {pdfDownloadUrl ? (
            <a
              href={pdfDownloadUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="pill pill-ghost pill-sm"
            >
              Download
            </a>
          ) : null}
        </div>

        {pdfDownloadUrl ? (
          <iframe
            src={`${pdfDownloadUrl}#toolbar=0&navpanes=0`}
            title="Master resume preview"
            style={{
              width: "100%",
              height: 560,
              border: "none",
              background: "var(--color-whisper-strong)",
            }}
          />
        ) : pdfFilename ? (
          <div
            className="flex items-center justify-center"
            style={{
              minHeight: 200,
              color: "var(--color-pebble)",
              fontSize: 13,
              background: "var(--color-whisper-strong)",
            }}
          >
            Preview unavailable (dev mode).
          </div>
        ) : null}
      </div>

      <label
        className="flex items-center justify-center cursor-pointer transition"
        style={{
          minHeight: 180,
          border: pdfFile
            ? "1px solid var(--color-hairline-strong)"
            : "1px dashed var(--color-hairline-strong)",
          borderRadius: 12,
          padding: 24,
          background: pdfFile ? "var(--color-whisper-strong)" : "transparent",
        }}
      >
        <input
          type="file"
          name="pdf_file"
          accept="application/pdf,.pdf"
          className="hidden"
          onChange={(e) => setPdfFile(e.target.files?.[0] ?? null)}
        />
        {pdfFile ? (
          <div className="flex flex-col items-center gap-2">
            <span className="mono" style={{ fontSize: 13, color: "#000" }}>
              {pdfFile.name}
            </span>
            <span className="mono" style={{ fontSize: 11, color: "var(--color-pebble)" }}>
              {(pdfFile.size / 1024).toFixed(0)} KB · click to replace
            </span>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-2">
            <span style={{ fontSize: 14, color: "#000" }}>
              Upload a new PDF to replace
            </span>
            <span className="mono" style={{ fontSize: 11, color: "var(--color-pebble)" }}>
              max 8 MB
            </span>
          </div>
        )}
      </label>

      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={pending || !pdfFile}
          className="pill pill-primary"
        >
          {pending ? "Reading…" : "Save new resume"}
        </button>
        {state?.ok ? <span className="status status-applied">Saved</span> : null}
        {state?.error ? (
          <span style={{ color: "#ff4704", fontSize: 13 }}>{state.error}</span>
        ) : null}
      </div>
    </form>
  );
}
