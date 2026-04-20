# applyd

Autonomous job-application engine for SWE/ML roles. Pipeline: **discover** jobs across aggregators + ATS APIs + search dorks → **enrich** with full descriptions via tiered cascade → **tailor** resume per job via Claude API → *(future)* contact discovery → *(future)* cold outreach.

Positioned for eventual open-source release: BYO credentials, single user per instance, self-hostable.

---

## Status

**Shipped:**
- Discovery (`src/applyd/discovery/`) — aggregators, broad-search via Brave dorks, user targets, resolver + caches
- Enrichment (`src/applyd/enrichment/`) — 4-tier cascade, threaded (ThreadPoolExecutor)
- Tailoring (`src/applyd/tailor/`) — Claude API with prompt caching, tectonic PDF compile, structural validator, structured JSON metadata output
- Filtering / CLI (`applyd discover | enrich | tailor | jobs | resolve`)

**Not yet built:**
- HITL review UI (FastAPI + HTMX dashboard for approving tailored drafts)
- Contact discovery (Sema vs Apollo — open question, see below)
- Outreach email pipeline (blocked on 3–4 week domain warm-up)
- Structured JD extraction (planned lazy, on first tailor per job)
- Graduation-year filter (user is Apr 2027 grad; most "new grad 2026" postings don't fit)

---

## CLI quick reference

```bash
applyd discover              # aggregators + broad search + targets.json companies
applyd enrich                # fetch full JD descriptions (threaded, --workers 8 default)
applyd tailor <job_id>       # LLM-tailored resume.tex + PDF + metadata.json
applyd jobs --level new_grad --specialty ml --remote   # query store
applyd resolve "Stripe"      # debug: company name → (ATS, slug)
```

Store: `data/jobs.json` (plain JSON dict keyed by stable job id).

---

## Architecture

Single-process Python. Pydantic `Job` model. JSON file store.

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
- `render.py` — Anthropic SDK call with prompt caching (system + base resume cached, JD fresh)
- `validate.py` — structural diff: no invented companies, education preserved, brace balance, header intact
- `compile.py` — tectonic wrapper → PDF

---

## Key design decisions (and why)

### Data
- **JSON file (`data/jobs.json`), not SQLite/Postgres.** Single writer, no network, zero ops. JSON is enough at current scale. When second writer appears: upgrade.
- **Opportunistic cache seeding.** Aggregator/broad-search URLs on ATS domains are parsed for `(company, ats, slug)` and pre-populate the resolver cache for free. See `_seed_cache_from_jobs` in `cli.py`.
- **Master resume is the sole source of truth.** No separate fact bank file. User is expected to include everything they've ever done in `resume_base.tex`; tailoring reorders/rephrases/drops but never adds experiences/metrics/tech not in master.

### Discovery
- **Three-layer discovery:** aggregator (free, broad) + broad-search (Brave dorks, finds companies not in user list) + user-specified company names (`targets.json`).
- **`targets.json` holds company names only.** Resolver figures out which ATS each is on via dork + URL parsing. User never specifies `greenhouse:stripe` — just `"Stripe"`.
- **Brave > Serper for our dorks.** Empirically tested on 16 companies: Brave 10/11 correct on known-ATS, Serper 9/11 with embarrassing misses on Stripe (returned `embed`) and Anthropic (returned `fullstackacademy`). Brave's index is independent; Serper is Google-derived and inherits DMCA risk from Google v. SerpAPI (Dec 2025).

### Enrichment
- **Try cheapest first.** Tier 1 (ATS API) handles ~40% of non-ATS-described jobs for free. Tier 2 (httpx+trafilatura) handles another ~10%. Only ~50% need paid spider.cloud.
- **Threaded, not async.** ThreadPoolExecutor at 8 workers. httpx.Client is thread-safe. async would be a bigger refactor for marginal gain; revisit only if we scale to a second use case.
- **Session-scoped `board_cache`.** When many jobs share a board (e.g. 15 SimplifyJobs URLs to `jobs.ashbyhq.com/runway-ml/*`), fetch that board once per run.

### Tailoring
- **Claude Sonnet 4.6 default.** Prompt caching is mature; cached reads ~10× cheaper. Writing quality on constrained rewrites is strong. Model is swappable via `TailorClient` abstraction.
- **LaTeX over DOCX.** Text-native (LLM reads/writes directly), diffable, one-binary compile (tectonic), no docxtpl fragility. ATS-parse risk mitigated by using single-column Jake's Resume template (not fancy two-column variants).
- **Dual output format: JSON metadata + ```latex fenced block.** Avoids JSON-escape hell for LaTeX's pervasive backslashes. Model emits `keywords_covered/missing`, `decisions_log`, `confidence`, `risk_flags` as JSON, then the resume in a fenced block.
- **Strict no-invention.** Reorder + rephrase + drop. The prompt explicitly forbids fabricating metrics, technologies, projects, scope. Prompt B–style "invention with judgment" rejected (risk: can't defend fabricated metric in interview; anchors the bullet in a way that makes real content feel thinner by contrast).

---

## Rejected paths — don't revisit without new info

- **DOCX via `docxtpl`.** Binary-file manipulation, package flakiness, debugging pain.
- **LinkedIn scraping.** Hard ToS violation; ~23% of automation accounts banned within 90 days (2026 data). Losing user's professional LinkedIn account costs more than any feature gain.
- **Self-hosted Playwright for tier 3.** Spider.cloud handles CSR + anti-bot at ~$0.0005/page. Our own browser pool doesn't earn its keep at <10k/month.
- **"Invention allowed" LLM prompts.** Even with guardrails, fabricated metrics fail in interviews and anchor the whole resume as untrustworthy.
- **Fact bank file separate from resume.** Redundant; master resume is sole source. User maintains one file.
- **Hardcoded per-ATS company lists.** Contradicts "general SWE agent" goal. targets.json is names only; resolver does the work.
- **State-machine library (`transitions`, `python-statemachine`).** At 8 states + 1 author, a plain enum column + audit table is enough. Revisit at second-engineer scale.
- **Celery / Redis Queue / Dagster / Prefect.** Enrichment at 50–3000 jobs/run is a for-loop + ThreadPoolExecutor.
- **SQLite right now.** JSON file scales fine at <50k jobs. Upgrade to SQLite or Postgres when we add a second writer (web UI, scheduled cron) — the JobStore interface is abstract enough to swap.

---

## Gotchas — things that burned us and the fixes

### Jake's Resume LaTeX template
- **`\input{glyphtounicode}` + `\pdfgentounicode=1` break tectonic.** pdflatex-specific primitives not in tectonic's default engine. Strip those two lines from the base resume. Quality impact on PDF Unicode copy-paste is negligible for ATS parsing.
- **Original Jake's template paste often missing `\resumeSubHeadingListEnd` after Experience section.** A long-standing copy-paste bug across many resumes. LaTeX doesn't fail until the end of the document. Add one before `\section{Projects}`.

### ATS API quirks
- **SmartRecruiters bulk list omits descriptions.** Hit `/v1/companies/{company}/postings/{internal_id}` per-job. The internal ID differs from the URL-path `refNumber` — match via bulk list, then use the matched job's `external_id` for the per-job call. Handled in `fetcher._fetch_smartrecruiters_description`.
- **Workable API changed.** Old `GET /api/v1/widget/accounts/{slug}?details=true` now returns `{jobs: []}` for every account. Live endpoint: `POST /api/v3/accounts/{slug}/jobs` with JSON body `{}`. Our `ats/workable.py` uses the new one.
- **Ashby has no public per-job endpoint.** Fetching `/posting-api/job-board/{company}` returns all jobs; filter by UUID client-side.
- **Ashby URLs ending in `/application` point to the form, not the JD.** Tier-1 ATS lookup handles this correctly via bulk-list-and-filter, as long as the job UUID is still live in the board.
- **SimplifyJobs URLs go stale.** A posting may still appear in `listings.json` after the ATS removes it. We re-hydrate via ATS API in tier 1 — stale postings legitimately return None from enrichment.

### Search APIs
- **SerpAPI is in an active DMCA lawsuit (Google, Dec 19 2025).** Don't build on it long-term. Brave is safer.
- **Serper's site-restricted dorks misidentify big brands.** Stripe → `embed`, Anthropic → `fullstackacademy`. Dealbreaker bug. Brave gets them right. Keep Brave default.

### Spider.cloud
- **"smart" mode sometimes picks HTTP for an SPA and returns the app shell (~25 chars).** Our cascade retries with explicit `request: "chrome"` as tier 3b.
- **Response shape varies** (dict vs list-of-one-dict). Normalized in `SpiderClient.scrape`.

---

## Environment

Required env vars (`.env` at repo root, auto-loaded by `applyd.config.load_env`):
- `BRAVE_SEARCH_API_KEY` — primary search provider
- `SPIDER_API_KEY` — tier 3 fetcher
- `ANTHROPIC_API_KEY` — tailoring

Optional:
- `SERPER_API_KEY` — fallback search
- `SEARCH_PROVIDER=brave|serper` — override default
- `BROAD_SEARCH_TTL_HOURS=6` — dork-result cache TTL

External tools:
- `tectonic` — LaTeX → PDF (`brew install tectonic`)

---

## Current store stats (as of last run)

- ~7,714 unique jobs discovered
- ~5,000 with descriptions from ATS bulk fetches (during `discover`)
- ~3,238 pending enrichment (~$0.45 spider budget + ~45 min threaded)

---

## Open questions

- **Contact discovery: Sema vs Apollo?** Sema pending scope confirmation with user's friend. Apollo's 2026 free tier turned out to be a 50-credit trial (not the 10k/month claimed by third-party blogs) — real Apollo usage requires the $49/mo Basic plan.
- **HITL review UI shape.** Current plan: FastAPI + HTMX + Jinja dashboard for resume review, Telegram bot for outreach approval. Not started.
- **Structured JD extraction.** Planned lazy (on first tailor per job), cached on Job record. Currently the tailor prompt does extraction inline — works, but means re-tailoring same job re-extracts.

---

## Collaboration style notes

When working in this repo with this user:
- Be direct and honest. User explicitly asked for "unbiased pair programmer" feedback.
- When the user pushes back, concede cleanly if they're right. Don't defend bad code.
- Keep responses tight. User gets overwhelmed by walls of text. Short bullets > long prose. Analogies or small tables > sprawling explanations.
- Disclose bias: when recommending Anthropic products (Claude, Claude Code, Claude Agent SDK), explicitly note you're made by Anthropic.
- Prefer "ship default + make it swappable" over "pick the best and commit forever." User values swap-ability.
- Don't add features, abstractions, or tests beyond what's asked. User tolerates sharp edges in v1.
- Ask one clarifying question if scope is ambiguous, then proceed. Don't ask two or three.

---

## Important context about the user

Divine Jojolola — Carleton University Bachelor of CS, **graduates April 2027**. Currently Shopify ML Infra intern (Sep 2025–present); heading to Lyft summer 2026. Targets applied ML at AI labs (Runway, Stability, Anthropic, Adobe Firefly) + general new-grad SWE as backup. Canadian.

The **Apr 2027 graduation date** matters for filtering — most "new grad 2026" postings (e.g. Scale AI, which wants Fall 2025/Spring 2026 grads) don't fit the user even when the tech stack matches. Graduation-year filter is a planned addition to the `jobs` command.
