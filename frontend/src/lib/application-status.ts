/**
 * User-facing collapse of the backend application-status enum.
 *
 * Backend tracks: pending | tailored | in_progress | applied | failed | skipped
 * User sees:      applied | pending | skipped         (failed is internal)
 *
 * Keep this single source of truth — pages read from here so we can change
 * the policy in one place if we ever decide to surface failures or split
 * pending further.
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

export const USER_STATUSES = ["applied", "pending", "skipped"] as const;
export type UserStatus = (typeof USER_STATUSES)[number];

/** Map a backend status to what the user sees, or null if it's hidden. */
export function toUserStatus(s: string): UserStatus | null {
  if (s === "applied") return "applied";
  if (s === "skipped") return "skipped";
  if (s === "pending" || s === "tailored" || s === "in_progress") return "pending";
  // "failed" and any unknown future status: hide from user.
  return null;
}

export const USER_STATUS_LABEL: Record<UserStatus, string> = {
  applied: "Applied",
  pending: "Pending",
  skipped: "Skipped",
};

/** Backend statuses that roll up into each user-facing bucket. Used to
 *  translate user filters → SQL queries. */
export const BACKEND_FOR_USER_STATUS: Record<UserStatus, BackendStatus[]> = {
  applied: ["applied"],
  pending: ["pending", "tailored", "in_progress"],
  skipped: ["skipped"],
};
