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

/** Backend statuses that roll up into each user-facing bucket. Used to
 *  translate user filters → SQL queries (the skipped/not_a_fit split then
 *  happens client-side on reason). */
export const BACKEND_FOR_USER_STATUS: Record<UserStatus, BackendStatus[]> = {
  applied: ["applied"],
  pending: ["pending", "tailored", "in_progress"],
  skipped: ["skipped"],
  not_a_fit: ["skipped"],
};
