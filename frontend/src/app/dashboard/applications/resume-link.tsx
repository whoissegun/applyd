"use client";

import { useState } from "react";
import { getApplicationResumeUrl } from "./actions";

/**
 * Per-application "view the resume we submitted" action. Signs the URL on click
 * (not on page load — 500 rows would mean 500 signatures that expire unused),
 * then opens the PDF in a new tab.
 */
export function ResumeLink({ applicationId }: { applicationId: string }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function open() {
    setLoading(true);
    setError(null);
    const res = await getApplicationResumeUrl(applicationId);
    setLoading(false);
    if (res.url) {
      window.open(res.url, "_blank", "noopener,noreferrer");
    } else {
      setError(res.error ?? "Unavailable");
    }
  }

  return (
    <button
      type="button"
      onClick={open}
      disabled={loading}
      className="hover:underline disabled:opacity-50"
      style={{ color: "var(--color-cosmic)", cursor: loading ? "default" : "pointer" }}
      title={error ?? "View the resume submitted for this application"}
    >
      {loading ? "Opening…" : error ? "Retry" : "View résumé"}
    </button>
  );
}
