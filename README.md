# applyd

Autonomous job-application engine for SWE/ML roles. Discovers openings across aggregators and ATS APIs, enriches them with full job descriptions, and tailors your LaTeX resume per posting using Claude. Self-hosted, BYO credentials, single-user.

> **Status:** v0.1 — core pipeline (discover → enrich → tailor) is working and has been exercised against real job boards. Contact discovery, cold outreach, and the human-in-the-loop review UI are not built yet. See [CLAUDE.md](CLAUDE.md) for architecture context and open questions.

---

## What it does

```
  ┌─────────────────────────────────────────────────────────────┐
  │  applyd discover                                            │
  │  ├── SimplifyJobs aggregator      (~2,500 SWE postings)     │
  │  ├── Brave broad-search dorks     (6 queries, 6h TTL cache) │
  │  └── user-specified companies     (targets.json, resolved)  │
  └─────────────────────────────────────────────────────────────┘
                              │
                              ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  applyd enrich        (threaded fetch cascade)              │
  │  ├── Tier 1: ATS bulk API              [free]               │
  │  ├── Tier 2: httpx + trafilatura       [free]               │
  │  ├── Tier 3a: spider.cloud smart       [~$0.0003/page]      │
  │  └── Tier 3b: spider.cloud chrome      [~$0.0005/page]      │
  └─────────────────────────────────────────────────────────────┘
                              │
                              ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  applyd tailor <job_id>                                     │
  │  ├── Claude Sonnet 4.6 (prompt-cached)                      │
  │  ├── Strict no-invention rewrite                            │
  │  ├── Structural validator                                   │
  │  └── tectonic → PDF                                         │
  └─────────────────────────────────────────────────────────────┘
```

## What's shipped

- [x] Discovery across 5 ATSes (Greenhouse, Lever, Ashby, Workable, SmartRecruiters) + SimplifyJobs + Brave search dorks
- [x] Per-company resolver (company name → ATS + slug) with persistent cache
- [x] Broad-search discovery cache (6h TTL) to avoid re-running dorks
- [x] 4-tier enrichment cascade with threaded concurrency
- [x] Resume tailoring with strict no-invention rules + structured JSON metadata output
- [x] Filtering by level / specialty / location / remote / source / company

## What's not shipped yet

- [ ] Human-in-the-loop review UI (dashboard to approve tailored drafts)
- [ ] Contact discovery (Sema / Apollo integration)
- [ ] Cold outreach email pipeline
- [ ] Structured JD extraction (requirements, years of experience, tech stack)
- [ ] Graduation-year gating in the filter layer

---

## Install

Requirements:
- Python 3.9+
- [tectonic](https://tectonic-typesetting.github.io/) for LaTeX → PDF compilation
  - macOS: `brew install tectonic`
  - Linux: see [install guide](https://tectonic-typesetting.github.io/en-US/install.html)
- API keys (see [Configure](#configure))

```bash
git clone git@github.com:whoissegun/applyd.git
cd applyd
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configure

Create a `.env` at the repo root:

```bash
# required
BRAVE_SEARCH_API_KEY=your_brave_key       # https://api-dashboard.search.brave.com/
SPIDER_API_KEY=your_spider_key            # https://spider.cloud/
ANTHROPIC_API_KEY=your_anthropic_key      # https://console.anthropic.com/

# optional
SERPER_API_KEY=your_serper_key            # https://serper.dev/ (fallback provider)
SEARCH_PROVIDER=brave                     # brave | serper (default: brave)
BROAD_SEARCH_TTL_HOURS=6                  # default 6
```

Put your base resume (in the [Jake's Resume LaTeX template](https://www.overleaf.com/latex/templates/jakes-resume/syzfjbzwjncs)) at `resume_base.tex`. **This is your single source of truth** — the tailor will reorder, rephrase, and drop content, but never invent new experiences. Include everything you've ever done that's worth mentioning; the tailor can cut what isn't relevant per-job.

Edit `targets.json` to list companies you want to track specifically:

```json
{
  "companies": ["Stripe", "Anthropic", "Runway", "OpenAI"],
  "broad_dorks": [
    "software engineer new grad",
    "ml engineer remote"
  ]
}
```

- `companies` — plain names. The agent resolves each to its ATS (Greenhouse / Lever / Ashby / Workable / SmartRecruiters) via a Brave dork and caches the mapping.
- `broad_dorks` — optional override for the 6 default broad-search queries. Omit this key to use defaults (which include `"software engineer intern"`, `"new grad software"`, etc.).

## Quickstart

```bash
# 1. Discover jobs
applyd discover
#   → pulls SimplifyJobs aggregator (~2,500 SWE postings)
#   → runs 6 broad Brave dorks (caches for 6h)
#   → resolves targets.json companies via Brave + fetches each ATS
#   → stores in data/jobs.json

# 2. Enrich with full JD text
applyd enrich --workers 8
#   → fetches descriptions for jobs that lack them
#   → tries free tiers first, falls back to spider.cloud for CSR / protected pages

# 3. Query the store
applyd jobs --level new_grad --specialty ml --remote
#   → filters and pretty-prints matching jobs with their ids

# 4. Tailor a resume for a specific job
applyd tailor simplifyjobs:<job_id>
#   → Claude rewrites resume_base.tex for this JD
#   → writes out/<company>-<role>/{resume.tex, resume.pdf, metadata.json}
#   → metadata includes keyword coverage, decisions log, risk flags
```

## CLI reference

| Command | Purpose |
|---|---|
| `applyd discover` | Pull from aggregators + broad search + user targets |
| `applyd enrich [--limit N] [--workers N] [--dry-run] [--retry-failed] [--source X]` | Fetch full JD text for jobs missing descriptions |
| `applyd tailor <job_id> [--no-compile] [--ignore-errors] [--model X]` | Generate a tailored resume for a specific job |
| `applyd jobs [--level] [--specialty] [--location] [--remote] [--source] [--company] [--limit] [--format]` | Query the job store |
| `applyd resolve <company> [--search-provider X]` | Debug: company name → (ATS, slug) |

---

## Rough cost expectations

At personal-use volume (one person, ~50 tailored applications/month):

| Service | Approximate monthly cost |
|---|---|
| Brave Search API | free ($5/mo credit covers ~1,000 queries) |
| spider.cloud | ~$0.50 one-time to enrich a full 3k-job corpus; ~$0.10/mo incremental |
| Anthropic Claude Sonnet 4.6 | ~$0.50–$2 for ~50 tailor calls (prompt caching reduces per-call cost ~10×) |
| **Total** | **~$1–$3/mo** |

Numbers are directional — actual cost depends heavily on how aggressively you run `enrich` and how many tailors per month.

---

## Architecture at a glance

Single-process Python. JSON file store. No database, no queue, no background workers.

```
src/applyd/
├── cli.py                    # argparse entrypoint
├── config.py                 # .env loader
├── models.py                 # Pydantic Job model
├── store.py                  # JSON file store
├── filters.py                # level/specialty/location filters
├── discovery/
│   ├── aggregators/          # simplifyjobs, broad_search
│   ├── ats/                  # greenhouse, lever, ashby, workable, smartrecruiters
│   ├── search/               # brave, serper (swappable)
│   ├── resolver.py           # company name → (ATS, slug)
│   ├── cache.py              # resolver + broad-search caches
│   └── routing.py            # URL → ATS detection
├── enrichment/
│   ├── fetcher.py            # 4-tier cascade
│   └── spider.py             # spider.cloud client
└── tailor/
    ├── prompts.py            # system prompt
    ├── render.py             # Anthropic SDK + prompt caching
    ├── validate.py           # structural diff (no invented companies, education preserved)
    └── compile.py            # tectonic wrapper
```

See [CLAUDE.md](CLAUDE.md) for deeper architecture notes, the list of rejected design paths (and why), and API gotchas we hit during development.

---

## Known limitations

- **Single-user, local execution.** No multi-tenancy. Intended for self-hosting.
- **Graduation-year filter not yet built.** "New Grad 2026" postings don't check whether you graduate in 2026 vs 2027 — you have to read the JD.
- **No outreach pipeline.** Sending cold email from a fresh domain needs 3–4 weeks of warm-up. We won't ship an auto-sender until that workflow is modeled properly.
- **Ambiguous company names** (e.g. "Mercury" the bank vs "Mercury Logistics Group") may resolve to the wrong ATS. Inspect `data/resolver_cache.json` after the first run and correct entries by hand if needed.
- **Stale aggregator URLs** (postings removed from the ATS between SimplifyJobs crawls) enrich to a `failed` state. Expected, not a bug — they're just dead postings.
- **SerpAPI is deliberately not supported** as the default search provider due to the active Google DMCA lawsuit (Dec 2025). Brave is the default; Serper is kept as a fallback only because its pricing is still viable.

---

## Roadmap

- HITL review dashboard (FastAPI + HTMX with keyboard-driven approve/edit/reject)
- Contact discovery (Sema or Apollo behind a swappable interface)
- Cold outreach email pipeline with IMAP reply detection
- Structured JD extraction (lazy per-job, cached on the `Job` record)
- Graduation-year gating in the filter layer
- Self-hosted LLM backend option (Ollama / Llama) for users who want to avoid paid APIs

---

## Credits

- Resume template: [Jake's Resume](https://github.com/jakegut/resume) by Jake Gutierrez (MIT).
- Discovery inspiration: [SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions) maintainers.
