# applyd

Autonomous job-application engine for SWE/ML roles. Discovers openings, enriches them with full job descriptions, tailors your LaTeX resume per posting, and has a browser agent fill out the apply form for you. Self-hosted today; multi-tenant SaaS is the next step.

> **Status (v0.1):** discovery + enrichment + tailor + apply all run end-to-end against real boards. The apply step migrated off OpenClaw to a stateless, library-shape direct runner — `Anthropic SDK + Playwright + Bright Data CDP` — so the same code can scale from one user on a laptop to a multi-tenant service. See [CLAUDE.md](CLAUDE.md) for deeper architecture notes and rejected paths.

---

## The pipeline

```
┌──────────────────────────────────────────────────────────────────────┐
│  applyd discover                                                     │
│  ├── SimplifyJobs aggregator       (~2,500 SWE postings, free)       │
│  ├── Brave broad-search dorks      (6h TTL cache)                    │
│  └── user-specified companies      (targets.json, resolved → ATS)    │
└──────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  applyd enrich         (threaded fetch cascade, ThreadPoolExecutor)  │
│  ├── Tier 1: ATS bulk API                  [free]                    │
│  ├── Tier 2: httpx + trafilatura           [free]                    │
│  ├── Tier 3a: spider.cloud smart           [~$0.0003/page]           │
│  └── Tier 3b: spider.cloud chrome          [~$0.0005/page]           │
└──────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  applyd tailor <job_id>                                              │
│  ├── Claude Sonnet 4.6 (prompt-cached)                               │
│  ├── Strict no-invention rewrite from resume_base.tex                │
│  ├── Structural validator (no fabricated companies / metrics)        │
│  ├── tectonic → PDF                                                  │
│  └── writes out/<slug>/{resume.tex, resume.pdf, metadata.json}       │
└──────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  applyd apply (direct runner)                                        │
│  ├── Picks next pending job (tailored, not gated, not attempted)     │
│  ├── Loads profile + tailored resume + JD metadata as context        │
│  ├── Anthropic SDK tool-use loop with prompt caching                 │
│  │   tools: navigate / read_form / fill / fill_many / click /        │
│  │          click_many / select_combobox / upload_file / submit /    │
│  │          report_done                                              │
│  ├── Connects via CDP to Bright Data residential Chrome              │
│  ├── Fills form, answers free-text grounded in resume + profile      │
│  └── Writes status + note inline to the job store (no callback HTTP) │
└──────────────────────────────────────────────────────────────────────┘
```

---

## What's shipped

- [x] Discovery across 5 ATSes (Greenhouse, Lever, Ashby, Workable, SmartRecruiters) + SimplifyJobs + Brave search dorks
- [x] Per-company resolver (company name → ATS + slug) with persistent cache
- [x] 4-tier enrichment cascade with threaded concurrency
- [x] Resume tailoring with strict no-invention rules + structured JSON metadata
- [x] Apply-gate detection — excludes Workday/Oracle/LinkedIn/Wellfound/etc. from apply pile before Claude tokens are spent
- [x] Filtering: `--level`, `--specialty`, `--location`, `--remote`, `--source`, `--company`, `--gated`/`--no-gated`
- [x] Direct apply runner (stateless library-shape) — Anthropic SDK + Playwright + Bright Data CDP, with prompt caching, idempotent navigate, batched `fill_many`/`click_many`, deterministic opener (forced `navigate` then `read_form`), and inline writeback to the job store
- [x] Free-text answers grounded in the tailored resume + profile (no hallucinated projects/metrics)

## What's not shipped yet

Tracked as issues — see [the issue tracker](https://github.com/whoissegun/applyd/issues) for current status, priorities, and design notes:

- [#2 Daily digest of applied/skipped/failed](https://github.com/whoissegun/applyd/issues/2)
- [#3 Real-time skip pings (Telegram/Discord)](https://github.com/whoissegun/applyd/issues/3)
- [#4 Cloud deployment (Docker + Hetzner/Fly/Railway)](https://github.com/whoissegun/applyd/issues/4)
- Multi-tenant migration — Postgres-backed job store, per-tenant profiles, chat UI for end-users (the single biggest in-flight piece; replaces `data/jobs.json` and the local `~/.openclaw/workspace/USER.md` dance with rows in a DB)
- [#5 Contact discovery: Sema vs Apollo](https://github.com/whoissegun/applyd/issues/5)
- [#6 Cold outreach email pipeline](https://github.com/whoissegun/applyd/issues/6)
- [#7 Structured JD extraction at enrichment time](https://github.com/whoissegun/applyd/issues/7)

---

## Single-user today, multi-tenant in flight

The direct apply runner is library-shape — you call `python -m applyd.apply.runner <job_id>` and it returns inline. No daemon, no callback server, no workspace files. That's the shape we need for multi-tenant SaaS, where each apply is a stateless task triggered by a queue worker.

Two things still tie applyd to a single user on a single machine:

- **`data/jobs.json`** is one process's source of truth. The next major change is swapping `JobStore` for a Postgres-backed store keyed by `(tenant_id, job_id)`. The interface is already abstract enough to swap.
- **Profile lives at `~/.openclaw/workspace/USER.md`** today (carried over from the legacy OpenClaw setup). Multi-tenant means profiles become DB rows passed into the runner per request, not a file on disk.

Both are scoped, not built. If you're running applyd for yourself today, neither matters.

---

## Install

Requirements:

- Python 3.9+
- [tectonic](https://tectonic-typesetting.github.io/) — LaTeX → PDF (`brew install tectonic` on macOS)
- Bright Data "Scraping Browser" zone — residential Chrome via CDP (handles stealth + captcha + IP rotation; the runner only sends CDP messages, no local Chrome needed)
- API keys: Anthropic, Brave, spider.cloud (see [Configure](#configure))

```bash
git clone git@github.com:whoissegun/applyd.git
cd applyd
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

> **Note on OpenClaw:** earlier versions of applyd ran the apply step through a local OpenClaw daemon. That path is deprecated and will be removed once the direct runner is fully proven. See [Apply step: legacy OpenClaw path](#apply-step-legacy-openclaw-path) at the bottom if you need it.

---

## Configure

### 1. `.env` at repo root

```bash
# required — discovery + enrichment + tailor + apply
BRAVE_SEARCH_API_KEY=...
SPIDER_API_KEY=...
ANTHROPIC_API_KEY=...

# required — apply step (Bright Data residential Chrome via CDP)
BRIGHTDATA_CUSTOMER_ID=...
BRIGHTDATA_ZONE=...
BRIGHTDATA_ZONE_PASSWORD=...

# strongly recommended while testing — agent fills but never clicks submit
APPLYD_TEST_MODE=true

# optional
SERPER_API_KEY=...                        # search fallback
SEARCH_PROVIDER=brave                     # brave | serper (default brave)
BROAD_SEARCH_TTL_HOURS=6
BRIGHTDATA_HOST=brd.superproxy.io         # default
BRIGHTDATA_CDP_PORT=9222                  # default

# legacy (only needed if you're using the old OpenClaw apply path)
OPENCLAW_TOKEN=...
APPLYD_CALLBACK_TOKEN=...
OPENCLAW_URL=http://127.0.0.1:18789/v1/chat/completions
APPLYD_CALLBACK_URL=http://127.0.0.1:9000/apply-result
APPLYD_DISPATCH_TIMEOUT=600
```

### 2. Base resume — `resume_base.tex` at repo root

Use the [Jake's Resume LaTeX template](https://www.overleaf.com/latex/templates/jakes-resume/syzfjbzwjncs). This file is **the single source of truth** for your experience. The tailor will reorder, rephrase, and drop content, but will never invent experiences, projects, metrics, or technologies not present here. Put everything you'd want any employer to potentially see — the tailor cuts what's irrelevant per job.

Two gotchas specific to tectonic:

- Remove `\input{glyphtounicode}` and `\pdfgentounicode=1` from the preamble — they're pdflatex-specific and break tectonic. Unicode copy-paste quality impact is negligible for ATS parsing.
- Confirm you have a `\resumeSubHeadingListEnd` after the Experience section before `\section{Projects}`. A common copy-paste of Jake's template omits it and LaTeX won't fail until the end of the document.

### 3. `targets.json` at repo root (companies you want tracked specifically)

```json
{
  "companies": ["Stripe", "Anthropic", "Runway", "OpenAI", "Palantir"],
  "broad_dorks": [
    "software engineer new grad",
    "ml engineer remote"
  ]
}
```

- `companies` — plain names. The resolver figures out the ATS + slug via a Brave dork and caches the mapping in `data/resolver_cache.json`.
- `broad_dorks` (optional) — overrides the 6 default broad queries. Omit to use defaults.

### 4. Profile — `~/.openclaw/workspace/USER.md` (today) or anywhere via `--profile`

The apply runner reads a profile file as prose context for every invocation. The path defaults to `~/.openclaw/workspace/USER.md` (a carry-over from the legacy OpenClaw setup; in multi-tenant this becomes a DB row). Override per run with `--profile /path/to/your.md`.

The file should cover everything the agent needs to fill a form:

- **Identity:** full legal name, preferred name, email, phone (with format hints), pronouns
- **Location:** current city, region, country; whether open to relocation
- **Links:** LinkedIn, GitHub, portfolio
- **Education:** school, degree, graduation month/year, GPA if worth including
- **Work authorization:** per-country (where you can work without sponsorship, where you need it). Be precise — the agent answers truthfully and refuses to fudge.
- **Demographics:** race, gender, veteran/disability status in the exact wording forms use. Include "decline to self-identify" as a valid answer.
- **Narrative hooks (optional but useful):** 3–5 bullets on "what I care about / what I'm curious about / the pattern of work I like." The agent uses these to anchor free-text answers like "Why us?" without slop.

A starter template lives at [`profile.example.md`](profile.example.md).

---

## Quickstart

```bash
# 1. Discover jobs
applyd discover
#   → SimplifyJobs + Brave dorks + targets.json companies → data/jobs.json

# 2. Enrich JD text for jobs without descriptions
applyd enrich --workers 8

# 3. Browse what's in the store
applyd jobs --no-gated --level new_grad --specialty ml

# 4. Tailor a resume for a specific job
applyd tailor <job_id>
#   → Claude tailors resume_base.tex
#   → writes out/<slug>/{resume.tex, resume.pdf, metadata.json}
#   → sets resume_pdf_path on the job

# 5. Run the direct apply runner against one job
python -m applyd.apply.runner <job_id> --test-mode=true
#   → loads profile + tailored resume + JD metadata
#   → connects to Bright Data CDP
#   → Claude tool-loops through the form (navigate → read_form →
#     fill_many → upload → click_many → submit → report_done)
#   → writes status + note inline to data/jobs.json
#   → returns 0 (applied) / 1 (skipped) / 2 (failed) / 3 (infra error)

# Real submission — only after a few test-mode runs eyeballed
python -m applyd.apply.runner <job_id> --test-mode=false
```

`--test-mode=true` (or `APPLYD_TEST_MODE=true` in `.env`) means the agent reaches the submit tool but the click is a no-op. Use it to validate the form fill end-to-end without actually submitting.

---

## CLI reference

| Command | Purpose |
|---|---|
| `applyd discover` | Pull from aggregators + broad search + user targets |
| `applyd enrich [--limit N] [--workers N] [--dry-run] [--retry-failed] [--source X]` | Fetch full JD text for jobs missing descriptions |
| `applyd tailor <job_id> [--no-compile] [--ignore-errors] [--model X] [--force]` | Generate tailored resume for a job (`--force` overrides gate check) |
| `applyd jobs [--level] [--specialty] [--location] [--remote] [--source] [--company] [--gated] [--no-gated] [--limit] [--format]` | Query the job store |
| `applyd resolve <company>` | Debug: company name → (ATS, slug) |
| `python -m applyd.apply.runner <job_id> [--test-mode true\|false] [--profile PATH] [--model X]` | **The apply step.** Direct runner — drives the form via Anthropic SDK + Playwright + Bright Data CDP. |
| `applyd callback` *(legacy)* | Run the HTTP callback server for the deprecated OpenClaw path |
| `applyd apply-one` *(legacy)* | Dispatch the next pending job to OpenClaw |

---

## Cost breakdown (rough estimates — still measuring)

These are ballparks pulled from a small handful of test runs against Ashby, plus our Bright Data dashboard (`Bandwidth - Browser API` line item). They will shift as we (a) cut the agent's wasted turns, (b) add rotating cache breakpoints across long conversations, and (c) hit ATSes with different DOM shapes (Greenhouse, Lever, Workable, Workday) where token + bandwidth profiles will differ. **Treat as directional, not contractual.**

### Per applied job

| Bucket | Estimate | Notes |
|---|---|---|
| LLM (Claude Sonnet 4.6 tool loop) | ~$0.22 | Cache-read of system + profile + resume × ~20 turns dominates. Fewer turns drops this proportionally. |
| Bright Data bandwidth ($8/GB) | ~$0.012 | ~1.5 MB/apply with `block_heavy=True` (we abort image/media/font requests). |
| Tailor (one-time per job, run separately) | ~$0.04 | Cached on the Job record. Re-applies for same job don't re-tailor. |
| **Per applied job** | **~$0.27** | LLM is ~80% of the bill. |

### Wall-clock

~2.3 minutes per apply on a single worker (measured on Ashby/Notion). Slower forms (Workday) likely longer. The runner is stateless, so `N` workers = `N×` throughput.

### Volume sketches

| Volume | Est. monthly $ | Worker-time/day |
|---|---|---|
| 50 applies/day | ~$420 | ~2 hr |
| 100 applies/day | ~$840 | ~4 hr (still fits one worker if you don't mind) |
| 500 applies/day | ~$4,200 | needs ~4 parallel workers |

### One-time / monthly fixed

| Service | Cost |
|---|---|
| Brave Search API | free tier covers personal volume (~1,000 queries/mo) |
| spider.cloud | ~$0.50 one-time to enrich a 3k-job corpus; ~$0.10/mo incremental |
| Anthropic (tailor across ~50 jobs) | ~$0.50–$2/mo |

### Where the numbers should drop next

- **Turn count.** Currently averaging ~20 turns/apply; the prompt baseline target is ~12. Each turn ≈ $0.011 in cache-read + fresh-input. Going 20 → 12 saves ~$0.09/apply (~33% off the LLM bucket).
- **Rotating cache breakpoints.** Right now only the static prefix (system + profile + resume) is cached; the growing conversation history is fresh on every turn. Adding a breakpoint every ~5 turns would cut fresh-input ~50% on long runs.
- **Bandwidth doesn't matter much.** Bright Data is ~5% of per-apply cost; not worth optimizing until LLM is squeezed.

---

## Repo layout

```
applyd/
├── resume_base.tex            # YOUR base resume — the source of truth for tailoring
├── targets.json               # companies you specifically want tracked
├── profile.example.md         # starter template for the apply-runner profile
├── requirements.txt           # pinned snapshot (pyproject.toml is the contract)
├── openclaw/                  # LEGACY — only used by the deprecated `apply-one` path
│   └── skills/applyd-apply/SKILL.md
├── src/applyd/
│   ├── cli.py                 # argparse + main() (lean — dispatch only)
│   ├── config.py              # .env loader
│   ├── models.py              # Pydantic Job model
│   ├── store.py               # JSON file store + pending_apply filter
│   ├── filters.py             # --level / --specialty / --gated filters
│   ├── callback.py            # LEGACY — FastAPI receiver for the OpenClaw skill
│   ├── apply/                 # the apply step
│   │   ├── browser.py         # Bright Data CDP URL builder + Playwright context
│   │   ├── tools.py           # tool dispatchers + TOOL_DEFS schema
│   │   ├── prompts.py         # system prompt + per-job user prompt builder
│   │   └── runner.py          # Anthropic tool-use loop; entry point
│   ├── commands/              # one CLI subcommand per file
│   │   ├── discover.py
│   │   ├── enrich.py
│   │   ├── tailor.py
│   │   ├── jobs.py
│   │   ├── resolve.py
│   │   └── apply.py           # LEGACY — apply-one + callback runner (OpenClaw)
│   ├── discovery/
│   │   ├── aggregators/       # simplifyjobs, broad_search
│   │   ├── ats/               # greenhouse, lever, ashby, workable, smartrecruiters
│   │   ├── search/            # brave, serper (swappable)
│   │   ├── resolver.py        # company name → (ATS, slug)
│   │   ├── cache.py           # resolver + broad-search caches
│   │   └── routing.py         # URL → ATS detection + gate detection
│   ├── enrichment/
│   │   ├── fetcher.py         # 4-tier cascade
│   │   └── spider.py          # spider.cloud client
│   └── tailor/
│       ├── prompts.py         # system prompt (strict no-invention)
│       ├── render.py          # Anthropic SDK + prompt caching
│       ├── validate.py        # structural diff (no fabricated companies / etc.)
│       └── compile.py         # tectonic wrapper
└── data/                      # NOT in git
    ├── jobs.json              # the store (grows to ~100 MB at ~8k jobs)
    ├── resolver_cache.json
    └── broad_search_cache.json
```

Key files if you're reading the code cold:

- `src/applyd/apply/runner.py` — the Anthropic tool-use loop, prompt caching, idempotent navigate, deterministic opener
- `src/applyd/apply/tools.py` — exactly which browser primitives the agent gets and what they return
- `src/applyd/apply/prompts.py` — system prompt + per-job context assembly
- `src/applyd/cli.py` — see every subcommand at a glance
- `src/applyd/discovery/routing.py` — ATS detection + the gated-domain blocklist
- `src/applyd/store.py` — the Job lifecycle & `pending_apply` filter (this is what gets swapped for Postgres in multi-tenant)

---

## Known limitations

- **`data/jobs.json` is single-process.** Fine for one user. Multi-tenant requires swapping `JobStore` for a Postgres-backed store; the interface is already abstract.
- **Profile lives at `~/.openclaw/workspace/USER.md` by default** (legacy holdover; override with `--profile`). Multi-tenant means profiles become DB rows passed in per request.
- **Apply step proven on Ashby; other ATS coverage unverified.** Greenhouse / Lever / Workable forms haven't been driven live yet — tools are generic but per-ATS quirks (react-select comboboxes, custom file inputs, multi-step Workday wizards) only surface on first contact.
- **Ambiguous company names** (e.g. "Mercury" the bank vs "Mercury Logistics Group") can resolve to the wrong ATS. Inspect `data/resolver_cache.json` after first run; edit by hand.
- **Stale aggregator URLs** (posting removed from the ATS between crawls) land in `fetch_tier="failed"`. Expected, not a bug.
- **Gated domains pre-filtered, not smart.** Workday / Oracle / Taleo / LinkedIn / Wellfound / etc. are skipped before tailor spend. Occasionally miscategorizes a direct-apply Workday; we leave money on the table for ~17% of discovered jobs.
- **SerpAPI deliberately unsupported** as default search provider (active Google DMCA lawsuit, Dec 2025). Brave is default; Serper kept only as a swappable fallback.
- **OpenClaw apply path is deprecated** but still in the tree (`commands/apply.py`, `callback.py`, `openclaw/skills/`). Will be deleted once the direct runner is fully validated across more ATSes.

---

## Apply step: legacy OpenClaw path

The original apply step ran through a local [OpenClaw](https://openclaw.ai/) daemon: `applyd apply-one` POSTed to the gateway, the `applyd_apply` skill drove an OpenClaw browser tool, and a separate `applyd callback` HTTP server received the result. That path still works but is deprecated — OpenClaw is designed for a single human, which is the wrong shape for a service that applies on behalf of many users.

If you specifically need the legacy path (e.g. for comparison / debugging):

- `applyd callback` — start the callback server (`:9000`)
- `applyd apply-one` — dispatch the next pending job to OpenClaw
- Requires `OPENCLAW_TOKEN` + `APPLYD_CALLBACK_TOKEN` in `.env`, an OpenClaw install with `skills.load.extraDirs` pointing at this repo's `openclaw/skills/`, and `playwright-core` installed inside the OpenClaw package.

Will be removed in a future release.

---

## Credits

- Resume template: [Jake's Resume](https://github.com/jakegut/resume) by Jake Gutierrez (MIT).
- Discovery inspiration: [SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions) maintainers.
- Apply runner: Anthropic SDK + Playwright + Bright Data Scraping Browser.
