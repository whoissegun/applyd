/**
 * Dev-only feature flags. Edit values here directly — no env vars.
 *
 * NODE_ENV guard makes every flag inert in production: Next inlines the
 * check at build time, so `next build` produces bundles where each
 * `devFlag(...)` is statically resolved to `false`. Mock fixtures below
 * never get pulled into a prod bundle for the same reason.
 */

const DEV = process.env.NODE_ENV === "development";

export const DEV_FLAGS = {
  /** Bypass auth + DB; render gated pages with mock data. */
  bypassAuth: false,

  /** Actually call Claude to extract resume content. Off → return a stub. */
  runExtraction: true,
} as const;

export function devFlag<K extends keyof typeof DEV_FLAGS>(key: K): boolean {
  if (!DEV) return false;
  return DEV_FLAGS[key];
}

/** Convenience for the most-checked flag. */
export const isDevBypass = (): boolean => devFlag("bypassAuth");

/* ──────────────────────────────────────────────────────────────────
   Mock fixtures — used only when `bypassAuth` is on.
   Add more here as new flags need supporting data.
   ────────────────────────────────────────────────────────────────── */

export const MOCK_USER_ID = "00000000-0000-0000-0000-000000000000";

export const MOCK_USER = {
  id: MOCK_USER_ID,
  email: "dev@applyd.local",
} as const;

export const MOCK_PROFILE = {
  full_name: "Test User",
  phone: "+1 555 555 0123",
  linkedin_url: "https://linkedin.com/in/test-user",
  github_url: "https://github.com/test-user",
  portfolio_url: "https://test-user.dev",
  profile_answers:
    "## Target roles\nNew-grad SWE, leaning ML infra and full-stack.\n\n## Work authorization\nAuthorized to work in Canada. Need sponsorship for the US.\nNeed sponsorship to work in: US",
};

export const MOCK_RESUME = {
  resume_text:
    "Divine Jojolola — Carleton CS, graduates Apr 2027.\n\nShopify (ML Infra Intern, Jan–Apr 2026)\n- Optimized similarity computation in Flink, cutting inference latency ~20% across 4 pipelines.\n- Built an Airflow DAG that reclaims stale ML data and syncs to GCS, freeing 6.9 TB across 61k files.\n\n(Dev preview — this content is mocked.)\n",
  master_pdf_storage_path: "00000000-0000-0000-0000-000000000000/master.pdf",
  updated_at: new Date().toISOString(),
};

const day = (n: number) =>
  new Date(Date.now() - n * 86_400_000).toISOString();

type MockApplication = {
  id: string;
  status: "applied" | "tailored" | "pending" | "skipped" | "failed" | "in_progress";
  job_id: string;
  last_attempt_at: string | null;
  applied_at: string | null;
  last_error: string | null;
  jobs: { title: string; url: string; companies: { canonical_name: string } };
};

export const MOCK_APPLICATIONS: MockApplication[] = [
  { id: "app-001", status: "applied",     job_id: "job-001", last_attempt_at: day(0), applied_at: day(0), last_error: null, jobs: { title: "Software Engineer, ML Platform", url: "https://example.com/1", companies: { canonical_name: "Stripe" } } },
  { id: "app-002", status: "applied",     job_id: "job-002", last_attempt_at: day(0), applied_at: day(0), last_error: null, jobs: { title: "Research Engineer",              url: "https://example.com/2", companies: { canonical_name: "Anthropic" } } },
  { id: "app-003", status: "tailored",    job_id: "job-003", last_attempt_at: day(1), applied_at: null,   last_error: null, jobs: { title: "Software Engineer Intern",       url: "https://example.com/3", companies: { canonical_name: "Lyft" } } },
  { id: "app-004", status: "applied",     job_id: "job-004", last_attempt_at: day(1), applied_at: day(1), last_error: null, jobs: { title: "Applied ML Engineer",            url: "https://example.com/4", companies: { canonical_name: "Runway" } } },
  { id: "app-005", status: "pending",     job_id: "job-005", last_attempt_at: null,   applied_at: null,   last_error: null, jobs: { title: "Backend Engineer",               url: "https://example.com/5", companies: { canonical_name: "Shopify" } } },
  { id: "app-006", status: "failed",      job_id: "job-006", last_attempt_at: day(2), applied_at: null,   last_error: "Form required cover letter.", jobs: { title: "ML Infra Engineer",              url: "https://example.com/6", companies: { canonical_name: "Cohere" } } },
  { id: "app-007", status: "in_progress", job_id: "job-007", last_attempt_at: day(0), applied_at: null,   last_error: null, jobs: { title: "Frontend Engineer",              url: "https://example.com/7", companies: { canonical_name: "Vercel" } } },
  { id: "app-008", status: "skipped",     job_id: "job-008", last_attempt_at: day(3), applied_at: null,   last_error: "JD mismatch.",               jobs: { title: "Software Engineer, Search",      url: "https://example.com/8", companies: { canonical_name: "Perplexity" } } },
];
