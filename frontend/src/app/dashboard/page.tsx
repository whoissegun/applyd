import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { isDevBypass, MOCK_USER, MOCK_APPLICATIONS } from "@/lib/devflags";
import { LocalDateTime } from "@/components/LocalDateTime";
import {
  USER_STATUSES,
  USER_STATUS_LABEL,
  statusChipLabel,
  toUserStatus,
  type UserStatus,
} from "@/lib/application-status";

export default async function DashboardOverview() {
  let user: { email: string } | null;
  let rows: {
    status: string;
    reason: string | null;
    last_attempt_at: string | null;
    jobs: { title: string | null; companies: { canonical_name: string | null } | null } | null;
  }[];

  if (isDevBypass()) {
    user = { email: MOCK_USER.email };
    rows = MOCK_APPLICATIONS.map((a) => ({
      status: a.status,
      reason: a.reason,
      last_attempt_at: a.last_attempt_at,
      jobs: a.jobs,
    }));
  } else {
    const supabase = await createClient();
    const {
      data: { user: u },
    } = await supabase.auth.getUser();
    user = u ? { email: u.email ?? "" } : null;

    const { data: apps } = await supabase
      .from("applications")
      .select("status, reason, applied_at, last_attempt_at, jobs(title, companies(canonical_name))")
      .order("last_attempt_at", { ascending: false, nullsFirst: false })
      .limit(500);
    rows = (apps ?? []) as unknown as typeof rows;
  }

  // Tally only user-visible statuses; failed rows are dropped here.
  const tallies: Record<UserStatus, number> = {
    applied: 0,
    pending: 0,
    skipped: 0,
    not_a_fit: 0,
  };
  for (const r of rows) {
    const us = toUserStatus(r.status, r.reason);
    if (us) tallies[us] += 1;
  }

  const visibleRows = rows.filter((r) => toUserStatus(r.status, r.reason) !== null);

  return (
    <div className="px-8 py-8 max-w-[1100px] mx-auto flex flex-col gap-10">
      <header className="flex items-end justify-between gap-6">
        <div>
          <p className="mono" style={{ color: "var(--color-pebble)" }}>{user?.email}</p>
          <h1 className="h-section mt-2">Overview</h1>
        </div>
      </header>

      <section className="grid grid-cols-4 gap-3">
        {USER_STATUSES.map((s) => (
          <div key={s} className="card p-5 flex flex-col gap-2">
            <span className="mono" style={{ fontSize: 11, color: "var(--color-pebble)" }}>
              {USER_STATUS_LABEL[s]}
            </span>
            <span
              className="font-display"
              style={{ fontSize: 36, fontWeight: 300, letterSpacing: "-0.02em", lineHeight: 1 }}
            >
              {tallies[s]}
            </span>
          </div>
        ))}
      </section>

      <section className="flex flex-col gap-4">
        <div className="flex items-end justify-between">
          <h2 className="h-card">Recent activity</h2>
          <Link href="/dashboard/applications" className="pill pill-ghost pill-sm">
            View all
          </Link>
        </div>

        {visibleRows.length === 0 ? (
          <div className="card p-8 text-center" style={{ color: "var(--color-pebble)" }}>
            <p>No applications yet.</p>
            <p className="mt-1" style={{ fontSize: "var(--text-sm)" }}>
              The matcher is finding jobs for you. Attempts show up here once it
              starts queuing.
            </p>
          </div>
        ) : (
          <div className="card-flush overflow-hidden">
            <table className="table">
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Company</th>
                  <th>Role</th>
                  <th>Last attempt</th>
                </tr>
              </thead>
              <tbody>
                {visibleRows.slice(0, 8).map((r, i) => {
                  const us = toUserStatus(r.status, r.reason)!;
                  return (
                    <tr key={i}>
                      <td>
                        <span className={`status status-${us}`}>{statusChipLabel(r.status, r.reason)}</span>
                      </td>
                      <td>{r.jobs?.companies?.canonical_name ?? "—"}</td>
                      <td>{r.jobs?.title ?? "—"}</td>
                      <td className="mono" style={{ color: "var(--color-cosmic)" }}>
                        <LocalDateTime iso={r.last_attempt_at} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
