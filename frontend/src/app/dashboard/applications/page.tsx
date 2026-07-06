import { createClient } from "@/lib/supabase/server";
import { isDevBypass, MOCK_APPLICATIONS } from "@/lib/devflags";
import { LocalDateTime } from "@/components/LocalDateTime";
import Link from "next/link";
import {
  BACKEND_FOR_USER_STATUS,
  USER_STATUS_LABEL,
  USER_STATUSES,
  toUserStatus,
  type UserStatus,
} from "@/lib/application-status";

type StatusFilter = "all" | UserStatus;

const FILTERS: { key: StatusFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "applied", label: "Applied" },
  { key: "pending", label: "Pending" },
  { key: "skipped", label: "Skipped" },
  { key: "not_a_fit", label: "Not a fit" },
];

type SearchParams = Promise<{ status?: string; q?: string }>;

export default async function ApplicationsPage({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const { status, q } = await searchParams;
  const active: StatusFilter =
    (FILTERS.find((s) => s.key === status)?.key as StatusFilter) ?? "all";

  type Row = {
    id: string;
    status: string;
    job_id: string;
    reason: string | null;
    last_attempt_at: string | null;
    applied_at: string | null;
    last_error: string | null;
    jobs: { title: string | null; url: string | null; companies: { canonical_name: string | null } | null } | null;
  };

  let data: Row[] | null;
  let error: { message: string } | null = null;

  if (isDevBypass()) {
    data = MOCK_APPLICATIONS as unknown as Row[];
  } else {
    const supabase = await createClient();
    let query = supabase
      .from("applications")
      .select(
        "id, status, job_id, reason, last_attempt_at, applied_at, last_error, jobs(title, url, companies(canonical_name))",
      )
      .order("last_attempt_at", { ascending: false, nullsFirst: false })
      .limit(500);
    // Hide failed rows from the user-facing view. We still write them in the
    // backend for debugging / billing — see application-status.ts.
    const visibleBackendStatuses = USER_STATUSES.flatMap(
      (s) => BACKEND_FOR_USER_STATUS[s],
    );
    query = query.in("status", visibleBackendStatuses);
    const res = await query;
    data = (res.data ?? null) as unknown as Row[] | null;
    error = res.error ? { message: res.error.message } : null;
  }

  // Drop failed/unknown rows up-front so they never reach the table or counts.
  const visibleRows = ((data ?? []) as Row[]).filter(
    (r) => toUserStatus(r.status, r.reason) !== null,
  );

  const rows = visibleRows.filter((r) => {
    if (active !== "all") {
      if (toUserStatus(r.status, r.reason) !== active) return false;
    }
    if (!q) return true;
    const hay = `${r.jobs?.title ?? ""} ${r.jobs?.companies?.canonical_name ?? ""}`.toLowerCase();
    return hay.includes(q.toLowerCase());
  });

  return (
    <div className="px-8 py-8 max-w-[1200px] mx-auto flex flex-col gap-8">
      <header className="flex items-end justify-between gap-6">
        <div>
          <p className="mono" style={{ color: "var(--color-pebble)" }}>{rows.length} rows</p>
          <h1 className="h-section mt-2">Applications</h1>
        </div>
      </header>

      <div className="flex flex-wrap items-center gap-2">
        <form className="flex items-center gap-2 mr-auto">
          <input
            name="q"
            defaultValue={q ?? ""}
            placeholder="Search company or role…"
            className="input w-[260px]"
          />
          {active !== "all" ? <input type="hidden" name="status" value={active} /> : null}
        </form>
        {FILTERS.map((s) => {
          const href = s.key === "all" ? "/dashboard/applications" : `/dashboard/applications?status=${s.key}`;
          return (
            <Link key={s.key} href={href} className="chip" data-active={s.key === active}>
              {s.label}
            </Link>
          );
        })}
      </div>

      {error ? (
        <div className="card p-6" style={{ color: "var(--color-ember)" }}>
          Couldn&apos;t load applications: {error.message}
        </div>
      ) : rows.length === 0 ? (
        <div className="card p-10 text-center" style={{ color: "var(--color-pebble)" }}>
          <p>No applications match this filter.</p>
        </div>
      ) : (
        <div className="card-flush overflow-hidden">
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 130 }}>Status</th>
                <th>Company</th>
                <th>Role</th>
                <th style={{ width: 200 }}>Last attempt</th>
                <th style={{ width: 160 }}>Job ID</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const userStatus = toUserStatus(r.status, r.reason)!;
                const detail = r.reason ?? r.last_error ?? undefined;
                return (
                  <tr key={r.id}>
                    <td>
                      <span className={`status status-${userStatus}`} title={detail}>
                        {USER_STATUS_LABEL[userStatus]}
                      </span>
                    </td>
                    <td>{r.jobs?.companies?.canonical_name ?? "—"}</td>
                    <td>
                      {r.jobs?.url ? (
                        <a
                          href={r.jobs.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="hover:underline"
                        >
                          {r.jobs.title ?? r.job_id}
                        </a>
                      ) : (
                        r.jobs?.title ?? r.job_id
                      )}
                    </td>
                    <td className="mono" style={{ color: "var(--color-cosmic)" }}>
                      <LocalDateTime iso={r.last_attempt_at} />
                    </td>
                    <td className="mono" style={{ color: "var(--color-steel)" }}>{r.job_id}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
