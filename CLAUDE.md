# applyd

Autonomous job-application engine for SWE/ML roles. Pipeline: **discover** jobs across aggregators + ATS APIs + search dorks → **enrich** with full descriptions via tiered cascade → **match** users to jobs (embeddings rank → LLM judges the top slice) → **tailor** resume per job → **apply** via headless browser → *(future)* contact discovery → *(future)* cold outreach.

**All LLM calls go through OpenRouter** (one `OPENROUTER_API_KEY`): classify, match, tailor, apply. The Anthropic SDK is no longer used. See [Key design decisions → LLM provider](#data).

Direction as of 2026-05-14: pivoting from personal-tool / single-tenant to **multi-tenant SaaS** on Supabase. Discovery/enrich/tailor pipelines are stateless and ready to thread `user_id` through; auth, per-user repos, apply layer, and frontend are the in-flight work.

---

## Status

**Shipped:**
- Discovery (`src/applyd/discovery/`) — aggregators, broad-search via Brave dorks, user targets, resolver + caches. Writer migrated to `JobsRepo`.
- Enrichment (`src/applyd/enrichment/`) — 4-tier cascade, threaded (ThreadPoolExecutor). Writer migrated to `JobsRepo`.
- Tailoring (`src/applyd/tailor/`) — Sonnet via OpenRouter with prompt caching (cache_control passthrough), tectonic PDF compile, structural validator, structured JSON metadata output. Multi-tenant entry point: `tailor_for_user(user_id, job_id)` in `tailor/saas.py`. CLI routed through this path. pdflatex-only preamble primitives are stripped before compile (see gotchas).
- Direct apply runner (`src/applyd/apply/`) — OpenRouter (OpenAI-compatible API) → tool-use loop → Playwright over Bright Data CDP. Multi-tenant entry: `apply_for_user(user_id, application_id)` in `apply/saas.py`; reads profile from `user_profiles.profile_answers`, pulls tailored PDF from Supabase Storage.
- FastAPI surface (`src/applyd/api/`) — JWT-auth'd endpoints over the SaaS paths.
- Apply worker (`src/applyd/worker/`) — `runner.py` polls `applications` for claimable rows and dispatches `apply_for_user`. `tailor_runner.py` and `matchmaker.py` cover the other two pools.
- LLM classify + match (`src/applyd/classify/`) — `job.py` infers `level`/`specialty` per job; `match.py` is the user-vs-job matcher.
- Per-user Supabase repos: `ApplicationsRepo`, `TailoredResumesRepo`, `ApplyAttemptsRepo`, `UserProfilesRepo`, `UserResumesRepo`, `UsageEventsRepo`, plus `pricing.py`.
- Supabase schema (10 migrations in `supabase/migrations/`): users/profiles/resumes/subscriptions, shared `companies`+`jobs` catalog, applications, tailored_resumes, apply_attempts, usage_events, `internal.*` worker caches, classification columns, race-handling/staleness. RLS on every public table; explicit grants (handles the 2026-04-28 Data API exposure breaking change); auto-create-profile + auto-create-subscription on signup via trigger on `auth.users`.

**In flight / not yet built:**
- Auth flow end-to-end (signup/login UI; backend trigger is wired). Frontend not chosen.
- Splitting per-user fields off the `Job` Pydantic model (`resume_pdf_path`, `apply_status`, `apply_attempted_at`, `apply_note`) — no remaining writers after JobStore deletion, safe to remove next.
- Frontend dashboard — likely Next.js + Supabase SSR; not started.
- Stripe billing / metered usage on `usage_events`.
- Cached structured JD extraction (Open question below — not started; requires prompt redesign).
- Daily digest, skip-pings channel, cloud deployment, contact discovery, outreach pipeline.

### Next session — priority order

1. **Frontend.** Next.js + Supabase SSR. Signup/login → profile editor → master resume upload → applications dashboard.
2. **Stripe billing.** Metered usage on `usage_events`; webhook → `user_subscriptions`.
3. **Drop the `Job` per-user fields.** `resume_pdf_path`, `apply_status`, `apply_attempted_at`, `apply_note` — already unused after the JobStore deletion; remove from the Pydantic model.
4. **Cached structured JD extraction.** Add `jobs.structured_jd jsonb`; on first tailor per job, write the JD analysis; rework the tailor prompt to accept pre-extracted analysis. Requires careful prompt diff to avoid regression.
5. **Cloud deployment.** API host + workers; secret management.

---

## CLI quick reference

```bash
applyd discover              # aggregators + broad search + targets.json companies
applyd enrich                # fetch full JD descriptions (threaded, --workers 8 default)
applyd tailor <job_id>       # LLM-tailored resume.tex + PDF + metadata.json
applyd jobs --level new_grad --specialty ml --remote   # query store
applyd resolve "Stripe"      # debug: company name → (ATS, slug)
```

Store today: Supabase Postgres. Shared `public.jobs` for the global catalog (writes via `JobsRepo`); per-user state in `applications` / `tailored_resumes` / `apply_attempts`. The legacy `data/jobs.json` + `JobStore` are gone.

---

## Architecture

Single-process Python today; the data layer is mid-migration to Supabase (Postgres + Auth + Storage + RLS).

### Layers

**1. Discovery** (`discovery/`) — populates the store.
- `aggregators/simplifyjobs.py` — crowdsourced SWE repo; free; ~2,500 postings per pull
- `aggregators/broad_search.py` — runs configurable Brave dorks, parses ATS URLs from results, fetches each ATS board; 6-hour TTL per-dork cache
- `ats/{greenhouse,lever,ashby,workable,smartrecruiters}.py` — per-company bulk fetchers
- `search/{brave,serper}.py` — swappable search providers via `SearchProvider` protocol
- `resolver.py` — `company name → (ATS, slug)` via dork + URL parsing
- `cache.py` — `ResolverCache` (no TTL) + `BroadSearchCache` (TTL)
- `routing.py` — URL domain → ATS detection; `parse_ats_url` pulls `(ats, slug, job_id)`

**2. Enrichment** (`enrichment/`) — populates `Job.description` for jobs that lack it.
- `fetcher.py` — tiered cascade:
  - Tier 1: ATS bulk API (free, uses session `board_cache` dict)
  - Tier 2: httpx + trafilatura (free, ~500ms)
  - Tier 3a: spider.cloud `smart` mode (~$0.0003/page)
  - Tier 3b: spider.cloud `chrome` mode (~$0.0005/page; explicit retry if 3a returns too-short content)
- `spider.py` — spider.cloud client
- SmartRecruiters has a special per-job-description fallback inside tier 1 (their bulk list omits descriptions)

**3. Tailor** (`tailor/`) — generates tailored resume.
- `prompts.py` — system prompt (aggressive tailoring, strict no-invention, structured JSON+latex output)
- `render.py` — OpenRouter (OpenAI SDK) call with prompt caching via cache_control passthrough (system + base resume cached, JD fresh)
- `validate.py` — structural diff: no invented companies, education preserved, brace balance, header intact
- `compile.py` — tectonic wrapper → PDF

**4. DB / Supabase** (`db/`) — the new data access layer.
- `client.py` — worker-side singleton using `SUPABASE_SECRET_KEY` (bypasses RLS). Frontends build their own with the publishable key + a user JWT.
- `jobs_repo.py` — shared-catalog reads/writes. Resolves company via `CompaniesRepo` internally; `upsert`, `get`, `mark_enriched`, `iter_pending_enrichment`.
- `companies_repo.py` — case-insensitive upsert by `canonical_name`.

---

## Key design decisions (and why)

### Data
- **Supabase (Postgres + Auth + RLS + Storage) is the multi-tenant backbone.** Picked over a Railway PG-only setup because auth, RLS, and resume blob storage come bundled — ~80% of the SaaS plumbing we'd otherwise build. Migrating off later is real but doable since the DB is just Postgres.
- **Jobs are a shared global pool, not per-user copies.** Same posting at Stripe is the same row for everyone. Discovery enriches once; all tenants benefit. Per-user state lives in `applications`.
- **Companies are normalized into their own table.** Same company gets discovered via aggregator + Brave dork + direct ATS scan; normalizing dedups and makes future company-level state (block lists, applied counts) easy.
- **Master resume is the sole source of truth.** No separate fact bank. Tailoring reorders/rephrases/drops but never invents.
- **Profile Q&A bank** = a single freeform `profile_answers text` column on `user_profiles`. UI structures the questions; DB stores raw text. No separate Q&A table.
- **`applications.job_id` is `ON DELETE SET NULL`**, not CASCADE. If we ever purge stale jobs, application history survives. Same for `tailored_resumes.job_id`.
- **Auto-provision on signup.** Trigger `on insert on auth.users` calls `internal.handle_new_auth_user()` (SECURITY DEFINER, locked search_path, lives in `internal` schema per Supabase security guidance) — creates `user_profiles` + `user_subscriptions` stub rows.

### Discovery
- **Three-layer discovery:** aggregator (free, broad) + broad-search (Brave dorks, finds companies not in user list) + user-specified company names (`targets.json`).
- **`targets.json` holds company names only.** Resolver figures out which ATS each is on via dork + URL parsing. User never specifies `greenhouse:stripe` — just `"Stripe"`.
- **Brave > Serper for our dorks.** Empirically tested on 16 companies: Brave 10/11 correct on known-ATS, Serper 9/11 with embarrassing misses on Stripe (returned `embed`) and Anthropic (returned `fullstackacademy`). Brave's index is independent; Serper is Google-derived and inherits DMCA risk from Google v. SerpAPI (Dec 2025).

### Enrichment
- **Try cheapest first.** Tier 1 (ATS API) handles ~40% of non-ATS-described jobs for free. Tier 2 (httpx+trafilatura) handles another ~10%. Only ~50% need paid spider.cloud.
- **Threaded, not async.** ThreadPoolExecutor at 8 workers. httpx.Client is thread-safe. async would be a bigger refactor for marginal gain.
- **Session-scoped `board_cache`.** When many jobs share a board, fetch that board once per run.

### LLM provider
- **Everything runs through OpenRouter (OpenAI-compatible API), one `OPENROUTER_API_KEY`.** Tailor uses `anthropic/claude-sonnet-4-6`; apply uses `anthropic/claude-haiku-4.5` (code constant `apply/runner.py::DEFAULT_MODEL`, not an env var — A/B'd July 2026, half Sonnet's price, same tool semantics); classify/match use `anthropic/claude-haiku-4.5`; embeddings use `openai/text-embedding-3-small`. Per-call model swap is free (`--model deepseek/...` etc.). Prompt caching still works via `cache_control` passthrough. Pricing keys live in `db/pricing.py` (canonical Anthropic ids double as OpenRouter slugs). The Anthropic SDK was fully removed June 2026 — don't reintroduce direct `anthropic.Anthropic()` calls.

### Matching (cost-critical)
- **Two-stage funnel, not one-LLM-call-per-job.** pgvector embeddings on `jobs.embedding` (classification text) + `user_profiles.embedding` (resume + LLM-extracted *positive* targets — exclusions dropped so negation doesn't pull the vector wrong). `rank_jobs_for_user()` ranks unseen jobs by cosine; the Haiku judge runs only on the top slice. A backlog stop-rule (`target_backlog`, default 30) halts judging once a user's apply queue is primed — spend tracks applications, not catalog size. Resume is the only hard requirement; `profile_answers` is optional.
- **The judge is batched** (`match_user_to_jobs`, `BATCH_SIZE=10` jobs per Haiku call) — the profile+resume tokens dominate the prompt and per-job calls re-sent them at full price. System + profile/resume blocks carry `cache_control` markers.
- **Free seniority prefilter before the judge.** `classification.seniority_signal` matching staff+/principal/director/manager/VP/8+-years goes straight to `skipped` with reason `prefilter:seniority` — no LLM call. Plain "senior / 5-8" still goes to the judge (it sees deal_breakers + full profile). This is an *operational* filter on already-classified data, not an enum taxonomy.
- **Barren-sweep cooldown.** A sweep that judges jobs and accepts nothing puts the user on an in-memory cooldown (`APPLYD_MATCHMAKER_BARREN_COOLDOWN`, default 6h) — the ranking tail is junk once accepts dry up; without this the matchmaker marches the whole catalog (July 2026: 6.1k judgments, 25 accepts, 98% rejects).
- **Persist the verdict row BEFORE billing usage, per-row try/except.** The applications row is the sole dedup marker; a sweep that crashed after billing re-judged the same top-ranked jobs every 5-min tick (June 2026: 32 jobs judged ~260× each = ~4.9k wasted calls, because the code wrote `failure_category` before its migration was applied).
- **Matcher rejects are `status='skipped'` + reason `matcher:`/`prefilter:`** — the dashboard renders them as "Not a fit", distinct from real apply-agent skips (see `frontend/src/lib/application-status.ts`).

### Tailoring
- **Free liveness check before the LLM call.** `tailor_for_user` runs `enrichment.fetcher.job_is_live(url)` (ATS bulk API) before spending; a definitive "not on the board" marks the job inactive for all tenants and skips the row (`dead_link_pre_tailor`). `None` (unknown ATS / fetch failed) proceeds. Catalog rows go stale routinely — the nightly `sweep_stale` safety net exists in `JobsRepo` but nothing schedules it.
- **Sonnet 4.6 via OpenRouter default.** Prompt caching is mature; cached reads ~10× cheaper. Writing quality on constrained rewrites is strong. Model swappable via `TailorClient`.
- **LaTeX over DOCX.** Text-native, diffable, one-binary compile (tectonic), no docxtpl fragility. ATS-parse risk mitigated by single-column Jake's Resume template.
- **Dual output format: JSON metadata + ```latex fenced block.** Avoids JSON-escape hell for LaTeX's pervasive backslashes.
- **Strict no-invention.** Reorder + rephrase + drop. Prompt explicitly forbids fabricating metrics, technologies, projects, scope.

---

## Rejected paths — don't revisit without new info

- **DOCX via `docxtpl`.** Binary-file manipulation, package flakiness, debugging pain.
- **LinkedIn scraping.** Hard ToS violation; ~23% of automation accounts banned within 90 days (2026 data). Losing user's professional LinkedIn account costs more than any feature gain.
- **Self-hosted Playwright for tier 3 enrichment.** Spider.cloud handles CSR + anti-bot at ~$0.0005/page. Our own browser pool doesn't earn its keep at <10k/month. (Apply layer uses Bright Data CDP — separate question.)
- **"Invention allowed" LLM prompts.** Even with guardrails, fabricated metrics fail in interviews and anchor the whole resume as untrustworthy.
- **Fact bank file separate from resume.** Redundant; master resume is sole source.
- **Hardcoded per-ATS company lists.** Contradicts "general SWE agent" goal. `targets.json` is names only.
- **State-machine library (`transitions`, `python-statemachine`).** Plain enum column + audit table is enough.
- **Celery / Redis Queue / Dagster / Prefect.** Enrichment at 50–3000 jobs/run is a for-loop + ThreadPoolExecutor.
- **SQLite or self-managed Postgres.** Supabase was chosen instead — auth/RLS/storage bundled.
- **A daemon-style agent runtime for the apply step.** Single-process daemons designed for one human's use don't fit multi-tenant SaaS. Stateless library-shape (`python -m applyd.apply.runner`) is what we have, and what scales.

---

## Gotchas — things that burned us and the fixes

### Jake's Resume LaTeX template
- **`\input{glyphtounicode}` + `\pdfgentounicode=1` break tectonic.** pdflatex-specific primitives not in tectonic's default engine. The model reproduces them from the classic Jake's preamble, so `tailor/saas.py::_strip_pdflatex_primitives` strips them before every compile (don't remove this — it's what stops the `glyphtounicode: Undefined control sequence` compile burn).
- **Original template often missing `\resumeSubHeadingListEnd` after Experience section.** LaTeX doesn't fail until end-of-document. Add one before `\section{Projects}`.

### ATS API quirks
- **SmartRecruiters bulk list omits descriptions.** Hit `/v1/companies/{company}/postings/{internal_id}` per-job. The internal ID differs from the URL-path `refNumber` — match via bulk list. Handled in `fetcher._fetch_smartrecruiters_description`.
- **Workable API changed.** Old `GET /api/v1/widget/accounts/{slug}?details=true` now returns `{jobs: []}` for every account. Live endpoint: `POST /api/v3/accounts/{slug}/jobs` with body `{}`.
- **Ashby has no public per-job endpoint.** Fetching `/posting-api/job-board/{company}` returns all jobs; filter by UUID client-side.
- **Ashby URLs ending in `/application` point to the form, not the JD.** Tier-1 ATS lookup handles this via bulk-list-and-filter as long as the UUID is still live.
- **SimplifyJobs URLs go stale.** A posting may still appear in `listings.json` after the ATS removes it. Tier 1 re-hydration filters these.

### Search APIs
- **SerpAPI is in an active DMCA lawsuit (Google, Dec 19 2025).** Don't build on it long-term. Brave is safer.
- **Serper's site-restricted dorks misidentify big brands.** Stripe → `embed`, Anthropic → `fullstackacademy`. Brave gets them right.

### Spider.cloud
- **"smart" mode sometimes picks HTTP for an SPA and returns the app shell (~25 chars).** Cascade retries with explicit `request: "chrome"` as tier 3b.
- **Response shape varies** (dict vs list-of-one-dict). Normalized in `SpiderClient.scrape`.

### Bright Data / Playwright
- **Never use `page.route()` on the Scraping Browser connection.** Client-side interception over `connect_over_cdp` is a known intermittent-hang class (microsoft/playwright#11776, ~30% of runs): every request round-trips through Python on the same websocket as click/evaluate, and a silent ws drop parks sync-Playwright forever (SIGALRM can't interrupt a greenlet transport wait). This caused the July 7 12h freeze and the `browser_in_use` cascade. Resource blocking is browser-side via CDP `Network.setBlockedURLs` (`apply/browser.py::BLOCKED_URL_PATTERNS`).
- **A Bright Data session is single-domain** (`navigate_domains_limit`): a cross-domain redirect (amazon.jobs → hiring portal) kills the session mid-run. No fix yet — such jobs terminal-skip.
- **Bright Data domain-blocks parts of the apply funnel** (verified 2026-07-16): legacy `boards.greenhouse.io` is robots.txt-restricted (error `brob`, sometimes `ERR_TUNNEL_CONNECTION_FAILED`), and some company sites (stripe.com) are compliance-blocked pending KYC (`brightdata.com/cp/kyc`). Modern `job-boards.greenhouse.io`, Lever, Ashby, Workable, SmartRecruiters all work. `preferred_apply_url` rewrites greenhouse-shaped URLs (legacy host, `gh_jid=` wrappers) to the `job-boards.greenhouse.io/embed/job_app` form, deriving the slug from `for=`/path/company-name-guess verified against the free boards API. Escalation path if more domains get blocked: KYC form / account manager.
- **Railway `restartPolicyMaxRetries` is a fixed per-deploy budget, not a rolling window.** The apply watchdog exits on purpose (hung CDP socket) and expects a restart; 10 watchdog exits marked the July 11 deploy CRASHED and the worker stayed down 5 days. Workers + API use `restartPolicyType = "ALWAYS"` — don't reintroduce a retry cap on long-running services.
- **Persist the verdict before the browser closes.** `browser.close()` over a stalled CDP socket can outlive the hard watchdog; an unpersisted `applied` verdict gets requeued and would re-submit a form the company already received. `run_apply(on_verdict=...)` fires with the full result at `report_done`, and `apply_for_user` persists there — before close.

### ATS anti-bot / reCAPTCHA email verification (2026-07-16)
- **The "enter this security code" emails are invisible reCAPTCHA, not a rate problem.** Greenhouse's invisible reCAPTCHA scores each *session* (mouse movement + typing cadence, per Google's v3: 0.0 bot → 1.0 human, pass ≈ 0.5). On a low score it doesn't hard-block — it emails a one-time code and injects an inline code field, asking to "enter the code and resubmit". Instant `.fill()` (zero keystrokes) + a browser that never moves the pointer is a maximal bot signature. Slowing the apply cadence does **not** help — the score is per-session. Behavioral realism does.
- **`APPLYD_HUMANIZE` (default true) is load-bearing, not cosmetic.** `apply/tools.py` types text fields with `press_sequentially` (jittered per-field cadence), moves the pointer before clicks, and adds think-time. Empirically: `humanize=false` scores so low reCAPTCHA won't even grant a token (submit hangs → 90s captcha timeout, no code wall); `humanize=true` gets a token but may still score low → the code wall appears (recoverable). So keep it on.
- **The wall is completed IN-SESSION, deterministically — we don't fight the score, we satisfy it.** `apply/tools.py::submit` checks for the wall on every poll iteration (it appears with NO navigation and NO reCAPTCHA token, so the old token/nav-only loop timed out and mislabeled it `applied`). On detection: read the emailed code, type it, resubmit — all before the browser closes. The code is bound to the live session; you cannot resume it later (a fresh form load generates a new code).
- **The code field is an 8-box OTP widget** (`security-input-0..7`, each `maxlength="1"`, auto-advancing). It MUST be filled with real keystrokes (`page.keyboard.type`) — `.fill()` drops all but the first char. `complete_email_verification` always keystrokes, independent of `APPLYD_HUMANIZE`. Verified end-to-end 2026-07-16 (Jump Trading → confirmation page).
- **Email is read over IMAP in the worker** (`apply/email_verify.py`), NOT the claude.ai Gmail connector (that's agent-only; the Railway worker can't reach it). Config: `APPLYD_IMAP_USER` + `APPLYD_IMAP_PASSWORD` (a Gmail **App Password**, not the account pw). Unset → `build_code_reader()` returns None → wall falls back to `gated:email_verification` (no false `applied`), so the feature is dormant-but-safe until creds are added. Sender `no-reply@us.greenhouse-mail.io`, subject `Security code for your application to <Company>`, code is 8 alnum chars in an `<h1>`; matched by company-in-subject + timestamp-after-submit. Verified full-loop with the real IMAP reader 2026-07-17 (Jump Trading → confirmation page, zero intervention).
- **IMAP `SINCE` is date-only AND evaluated in the mail server's timezone, not UTC.** `SINCE <utc-today>` silently drops a code email that arrived at e.g. 00:59 UTC because Gmail's server (behind UTC) still files it under "yesterday" — this broke code fetching for every apply in the ~00:00–08:00 UTC window. `email_verify.py` subtracts a day from the SINCE date for margin and relies on the precise Python-side `sent >= after` filter for correctness. Don't tighten SINCE back to `after`'s own date.
- **Multi-tenant gap:** one global IMAP mailbox only works while every apply uses one contact email. Per-user email access (OAuth or platform +alias inbox) is the SaaS path — deferred.
- **Pacing is cheap insurance, not the fix.** `worker/runner.py` adds jittered inter-apply gaps (`APPLYD_APPLY_JITTER_{MIN,MAX}_SECONDS`, default 8–25s) so the cadence isn't a metronome, and an optional `APPLYD_APPLY_DAILY_CAP` (default 0 = unlimited, preserving volume-first). The complex per-ATS/per-company selective-claim pacing was deliberately NOT built — walls are recoverable now, so it's low-value/higher-risk.

### Supabase
- **New tables in `public` are not auto-exposed to the Data API** (breaking change 2026-04-28). Migrations must `GRANT` explicitly to `anon`/`authenticated`. Initial schema migration handles this; future tables must follow suit.
- **`SECURITY DEFINER` functions don't go in `public`.** Put them in `internal` and `set search_path = ''`. The signup trigger function is in `internal.handle_new_auth_user()`.
- **Supabase emits 5-digit microseconds.** `datetime.fromisoformat` only handles these on Python 3.11+; the project pins `requires-python = ">=3.11"` so we use the stdlib parser directly. Don't add `python-dateutil` back.

---

## Apply layer

Direct runner at `src/applyd/apply/`. Library-shape, single-process: call it, it returns.

```
python -m applyd.apply.runner <job_id> [--model <slug>] [--profile <path>] [--test-mode true|false]
                         │
                         │ OpenAI SDK ──► OpenRouter (default: anthropic/claude-sonnet-4-6)
                         ▼
                   tool-use loop (TOOL_DEFS in apply/tools.py)
                         │
                         │ CDP
                         ▼
                   Playwright ──► Bright Data Chrome ──► apply form
                         │
                         ▼
                   JobStore.mark_apply(status, note)  [today]
                   ApplicationsRepo / ApplyAttemptsRepo  [once auth lands]
```

- **OpenRouter, not direct Anthropic SDK**, so the model is swappable per run (`--model deepseek/...`, `meta-llama/...`, etc.). Same `OPENROUTER_API_KEY` powers the whole pipeline.
- **`APPLYD_TEST_MODE=true`** keeps the agent from clicking submit — fills the form, screenshots optional, returns "would have submitted." Flip to `false` only after you've eyeballed test-mode runs.
- **Profile**: `--profile <path>`, defaults to `./profile.md`. SaaS migration moves this to a row in `user_profiles.profile_answers`.
- **Failures/skips only**: no screenshots or transcripts on success. ATS confirmation emails are the receipt of record.
- **Job-level skip verdicts propagate to the shared catalog** (`apply/saas.py::_propagate_job_gate`): `gated:dead_link` → `jobs.active=false`; login/signup walls, mandatory cover letters, coding challenges → `jobs.apply_gate` (ranker filters both). Captcha and profile-specific skips (`missing_info`, `jd_mismatch`) stay per-user.
- **OpenRouter 403 "Key limit exceeded" is transient** (`llm_errors.py`): it's account-wide like 402 no-credits — workers requeue + back off. Plain 403s stay terminal.
- **Sliding prompt cache via OpenRouter automatic caching** (July 2026): the runner sends top-level `cache_control: {"type":"ephemeral"}` + pins `provider: {order: ["anthropic"]}` for `anthropic/*` models. Before this, only the static prefix was cached and the growing tool-loop transcript was re-sent at full price every turn — 83% of all apply spend. Don't remove the provider pin: the cache lives at the upstream provider, and cross-provider routing forfeits every hit. Keep `TOOL_DEFS` byte-stable across turns (any change invalidates the whole cache).
- **`usage_events.metadata.openrouter_cost_usd`** is what OpenRouter actually billed (from `usage: {include: true}`); `cost_cents` is our computed figure (now includes cache writes at 1.25×). Aggregate via the `usage_daily` / `usage_monthly` / `apply_spend_by_outcome` views.
- **CDP connect failures are transient infra, not job failures.** Bright Data refuses reconnects with `browser_in_use` for up to ~5 min after an unclean worker death (no remote kill API exists). `brightdata_page` retries once, then raises `BrowserConnectError` → requeue + worker backoff. Attempts closed with an `infra:` reason are excluded from the `MAX_APPLY_ATTEMPTS` cap.
- **`hit MAX_TURNS` failures are terminal on the first hit** — retrying the same form with the same model fails identically at ~$1/run (one app burned $3.30 across three retries before this rule).

Multi-tenant work the runner needs:
- Thread a `user_id` parameter through `runner.py`'s entry.
- Replace `JobStore` reads with `JobsRepo.get(job_id)` + auth context.
- Replace `JobStore.mark_apply` with `ApplicationsRepo.update_status` + `ApplyAttemptsRepo.insert`.
- Replace profile-file reads with `user_profiles.profile_answers` from DB.
- Browser provider: Bright Data CDP per-tenant. Decision documented; not revisiting without new info.

---

## Environment

Required env vars (`.env` at repo root, auto-loaded by `applyd.config.load_env`):
- `BRAVE_SEARCH_API_KEY` — primary search provider
- `SPIDER_API_KEY` — tier 3 enrichment fetcher
- `OPENROUTER_API_KEY` — **all** LLM calls: classify, match, tailor, apply (and embeddings). The single LLM gateway. (`ANTHROPIC_API_KEY` is no longer used.)
- `BRIGHTDATA_CUSTOMER_ID`, `BRIGHTDATA_ZONE`, `BRIGHTDATA_ZONE_PASSWORD`, `BRIGHTDATA_HOST`, `BRIGHTDATA_CDP_PORT` — Bright Data Scraping Browser; will be used by the rebuilt apply layer if we pick the self-hosted Playwright path
- `SUPABASE_URL` — project HTTPS gateway (`https://<ref>.supabase.co`)
- `SUPABASE_PUBLISHABLE_KEY` — replaces legacy `anon`; safe to expose in frontends
- `SUPABASE_SECRET_KEY` — replaces legacy `service_role`; server/workers only, bypasses RLS
  - tailor default `anthropic/claude-sonnet-4-6`; apply `anthropic/claude-haiku-4.5` (code constant, see LLM provider section); classify/match `anthropic/claude-haiku-4.5`; embeddings `openai/text-embedding-3-small`
- `APPLYD_TEST_MODE=true|false` — when `true`, the apply runner stops short of submitting. Default to `true` during scale-up

Optional:
- `SERPER_API_KEY` — fallback search
- `SEARCH_PROVIDER=brave|serper` — override default
- `BROAD_SEARCH_TTL_HOURS=6` — dork-result cache TTL
- `APPLYD_IMAP_USER`, `APPLYD_IMAP_PASSWORD` — mailbox for reading ATS security codes (Gmail App Password). Unset → the reCAPTCHA email-verification wall falls back to `gated:email_verification`. `APPLYD_IMAP_HOST` (default `imap.gmail.com`), `APPLYD_IMAP_PORT` (993).
- `APPLYD_HUMANIZE=true|false` (default true) — human-like typing/mouse/think-time in the apply browser. Load-bearing for reCAPTCHA scores AND for the OTP code field; only disable for debugging.
- `APPLYD_APPLY_JITTER_MIN_SECONDS` / `APPLYD_APPLY_JITTER_MAX_SECONDS` (default 8/25) — randomized gap between applies.
- `APPLYD_APPLY_DAILY_CAP` (default 0 = unlimited) — hard ceiling on applies/day.
- `APPLYD_APPLY_MAX_SECONDS` (default 420) — per-apply wall-clock; raised from 300 to leave room for the in-session email-verification wait.

External tools:
- `tectonic` — LaTeX → PDF (`brew install tectonic`)
- `supabase` CLI — migrations + project link (`brew install supabase/tap/supabase`)

---

## Current store stats (as of last single-tenant run)

- ~7,714 unique jobs discovered
- ~5,000 with descriptions from ATS bulk fetches (during `discover`)
- ~3,238 pending enrichment (~$0.45 spider budget + ~45 min threaded)

---

## Open questions

- **Contact discovery: Sema vs Apollo?** Sema pending scope confirmation with user's friend. Apollo's 2026 free tier is a 50-credit trial; real usage needs the $49/mo Basic plan.
- **BYO-API-keys vs platform-pays-and-meters.** SaaS direction usually pushes platform-pays + Stripe metered billing on `usage_events`. Not decided.
- **Frontend stack.** Next.js + Supabase SSR is the path-of-least-resistance; not committed.
- **HITL review UI.** Likely Next.js dashboard. Not started.
- **Structured JD extraction.** Planned lazy (on first tailor per job), cached on the job row. Currently inline in the tailor prompt — works but re-extracts on re-tailor.

---

## Collaboration style notes

When working in this repo with this user:
- Be direct and honest. User explicitly asked for "unbiased pair programmer" feedback.
- When the user pushes back, concede cleanly if they're right. Don't defend bad code.
- Keep responses tight. User gets overwhelmed by walls of text. Short bullets > long prose. Small tables > sprawling explanations.
- Disclose bias: when recommending Anthropic products (Claude, Claude Code, Claude Agent SDK), explicitly note you're made by Anthropic.
- Prefer "ship default + make it swappable" over "pick the best and commit forever."
- Don't add features, abstractions, or tests beyond what's asked. Sharp edges in v1 are fine.
- Ask one clarifying question if scope is ambiguous, then proceed. Don't ask two or three.

---

## Important context about the user

Divine Jojolola — Carleton University Bachelor of CS, graduates April 2027. Currently Shopify ML Infra intern (Jan 2026 – Apr 2026), prior Shopify SWE intern (Sep 2025 – Dec 2025); heading to Lyft summer 2026. Targets applied ML at AI labs (Runway, Stability, Anthropic, Adobe Firefly) + general new-grad SWE as backup. Nigerian national on a Canadian study/work permit — eligible to work in Canada; needs sponsorship for US/Europe/elsewhere.

**Apply strategy is volume-first.** No grad-year filter, no location filter, no on-site/remote filter. Skip only on dedupe (already applied) or dead link. Work-auth questions answered truthfully (needs sponsorship outside Canada); US postings refusing sponsorship will reject — that's the cost of being in the funnel. Goal is maximum applications, not precision targeting.
