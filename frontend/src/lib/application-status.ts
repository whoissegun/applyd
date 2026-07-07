/**
 * User-facing collapse of the backend application-status enum.
 *
 * Backend tracks: pending | tailored | in_progress | applied | failed | skipped
 * User sees:      applied | pending | skipped | not_a_fit   (failed is internal)
 *
 * "skipped" and "not_a_fit" share the backend status 'skipped' and are split
 * by the reason column: matcher/prefilter rejects were never real application
 * attempts — surfacing 1,200 of them as "Skipped applications" is how we
 * learned to make this distinction. Apply-agent skips (dead link, captcha,
 * login wall) are genuine attempts that couldn't complete.
 *
 * Keep this single source of truth — pages read from here so we can change
 * the policy in one place.
 */

export const BACKEND_STATUSES = [
  "pending",
  "tailored",
  "in_progress",
  "applied",
  "failed",
  "skipped",
] as const;
export type BackendStatus = (typeof BACKEND_STATUSES)[number];

export const USER_STATUSES = ["applied", "pending", "skipped", "not_a_fit"] as const;
export type UserStatus = (typeof USER_STATUSES)[number];

/** Reason prefixes written by the pre-apply filtering stages (matchmaker
 *  judge, seniority prefilter). Rows with these were never attempted. */
const PRE_APPLY_REASON_PREFIXES = ["matcher:", "prefilter:"];

/** Map a backend status (+ reason) to what the user sees, or null if hidden. */
export function toUserStatus(s: string, reason?: string | null): UserStatus | null {
  if (s === "applied") return "applied";
  if (s === "skipped") {
    const r = reason ?? "";
    return PRE_APPLY_REASON_PREFIXES.some((p) => r.startsWith(p))
      ? "not_a_fit"
      : "skipped";
  }
  if (s === "pending" || s === "tailored" || s === "in_progress") return "pending";
  // "failed" and any unknown future status: hide from user.
  return null;
}

export const USER_STATUS_LABEL: Record<UserStatus, string> = {
  applied: "Applied",
  pending: "Pending",
  skipped: "Skipped",
  not_a_fit: "Not a fit",
};

/** Finer labels within the "pending" bucket, so a queued row shows *which*
 *  stage it's in rather than a flat "Pending". Backend statuses map:
 *    pending     → resume not tailored yet (or being tailored)
 *    tailored    → resume ready, waiting for the apply worker
 *    in_progress → apply agent is filling the form right now */
const PENDING_STAGE_LABEL: Record<string, string> = {
  pending: "Tailoring resume",
  tailored: "Queued to apply",
  in_progress: "Applying",
};

/** Label for a status chip: the fine-grained stage inside "pending", else the
 *  bucket label. Tallies/filters still use the coarse USER_STATUS_LABEL. */
export function statusChipLabel(backendStatus: string, reason?: string | null): string {
  const us = toUserStatus(backendStatus, reason);
  if (us === "pending") return PENDING_STAGE_LABEL[backendStatus] ?? "Pending";
  return us ? USER_STATUS_LABEL[us] : "";
}

/** Backend statuses that roll up into each user-facing bucket. Used to
 *  translate user filters → SQL queries (the skipped/not_a_fit split then
 *  happens client-side on reason). */
export const BACKEND_FOR_USER_STATUS: Record<UserStatus, BackendStatus[]> = {
  applied: ["applied"],
  pending: ["pending", "tailored", "in_progress"],
  skipped: ["skipped"],
  not_a_fit: ["skipped"],
};

/** Fixed skip verdicts written by the pipeline (tailor liveness check, apply
 *  agent, gate propagation). Matched by prefix — several carry a ` | detail`
 *  suffix meant for debugging, not the user. */
const SKIP_REASON_LABELS: [prefix: string, label: string][] = [
  ["prefilter:seniority", "Role is too senior for your profile"],
  ["gated:dead_link", "Job posting is no longer live"],
  ["dead_link_pre_tailor", "Job posting is no longer live"],
  ["gated:login_required", "Application requires logging into an account"],
  ["gated:signup_required", "Application requires creating an account"],
  ["gated:cover_letter_required", "Application requires a cover letter"],
  ["skipped:coding_challenge", "Application requires a coding challenge"],
  ["gated:captcha", "Application is blocked by a captcha"],
  ["gated:missing_info", "Application asks for info missing from your profile"],
  ["skipped:jd_mismatch", "Role requirements don't match your profile"],
];

/** Human-readable reason for a skipped / not-a-fit row, or null when there's
 *  nothing worth showing (non-skip statuses, empty reason). */
export function humanizeSkipReason(reason?: string | null): string | null {
  const r = (reason ?? "").trim();
  if (!r) return null;
  // Matcher judge rejects carry free text after the prefix — show it as-is.
  if (r.startsWith("matcher:")) {
    const text = r.slice("matcher:".length).trim();
    return text || "Matcher judged this role a poor fit";
  }
  for (const [prefix, label] of SKIP_REASON_LABELS) {
    if (r.startsWith(prefix)) {
      // jd_mismatch / missing_info carry a useful detail suffix
      // (`| reason='…'` / `| field='…'`) — surface the quoted part.
      const detail = r.match(/\|\s*(?:reason|field)='([^']+)'/)?.[1];
      return detail ? `${label} (${detail})` : label;
    }
  }
  // Unknown reason string: show it raw rather than hide it.
  return r;
}
