"use client";

import { useEffect, useState } from "react";

/**
 * Render an ISO timestamp in the *viewer's* local timezone.
 *
 * The dashboard pages are Server Components, so a plain
 * `new Date(iso).toLocaleString()` runs on the server (UTC) and ships a frozen
 * UTC string — which reads as "tomorrow" for anyone west of UTC. Formatting has
 * to happen in the browser. We format after mount so server and first client
 * render agree (no hydration mismatch); `suppressHydrationWarning` covers the
 * post-mount swap.
 */
export function LocalDateTime({
  iso,
  prefix = "",
  fallback = "—",
}: {
  iso: string | null | undefined;
  prefix?: string;
  fallback?: string;
}) {
  const [text, setText] = useState<string | null>(null);

  useEffect(() => {
    setText(iso ? prefix + new Date(iso).toLocaleString() : fallback);
  }, [iso, prefix, fallback]);

  // Before mount: render the fallback (never a UTC string), so no-JS and the
  // first paint show something neutral rather than the wrong day.
  return <span suppressHydrationWarning>{text ?? (iso ? "…" : fallback)}</span>;
}
