# applyd

Autonomous job-application engine for SWE/ML roles. Discovers openings, enriches them with full job descriptions, tailors your LaTeX resume per posting, and (in progress) has a browser agent fill out the apply form for you. Self-hosted, BYO credentials, single-user.

> **Status (v0.1):** discovery + enrichment + tailor are production-running and hitting real boards. The apply step is wired end-to-end and has completed test-mode fills (tailored resume uploaded, free-text questions answered, screenshot saved) but has not yet been flipped to submit-for-real. See [CLAUDE.md](CLAUDE.md) for deeper architecture notes and rejected paths.

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
│  applyd apply-one                                                    │
│  ├── picks next tailored job that isn't gated / already attempted    │
│  ├── POSTs dispatch payload → OpenClaw gateway (local daemon)        │
│  └── blocks waiting for the agent's final response (≤600s)           │
│                                                                      │
│  Meanwhile, inside OpenClaw:                                         │
│  ├── auto-injects USER.md + the applyd_apply skill                   │
│  ├── Claude tool-loops through OpenClaw's browser tool               │
│  ├── browser connects via CDP to Bright Data residential Chrome      │
│  ├── fills form, answers free-text questions from tailored resume    │
│  ├── in test_mode: screenshots, does NOT submit                      │
│  └── curl POST → applyd callback server                              │
│                                                                      │
│  applyd callback (separate daemon on :9000)                          │
│  └── receives {job_id, status, note}, writes to data/jobs.json       │
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
- [x] OpenClaw skill that handles arbitrary free-text answers grounded in the tailored resume (no hallucinated projects/metrics)
- [x] Callback server + dispatcher; full apply loop runs end-to-end in test mode

## What's not shipped yet

- [ ] First real submit-for-real apply (gated behind more test-mode visual verification)
- [ ] Daily digest (summary of applied/skipped/failed)
- [ ] Real-time skip pings (Telegram/Discord)
- [ ] Cloud deployment (Railway / Fly / Docker)
- [ ] Contact discovery (Sema vs Apollo — open)
- [ ] Cold outreach email (blocked on 3–4 week domain warm-up)
- [ ] Structured JD extraction at enrichment time (cached on Job)

---

## Current hard constraint: runs only on *this* machine

applyd is not portable as-is. It relies on several absolute paths and a specific OpenClaw install that are hardcoded to my laptop:

- `~/.openclaw/openclaw.json` must have `skills.load.extraDirs` pointing at **this repo's** `openclaw/skills/` directory — the path is absolute.
- `~/.openclaw/workspace/USER.md` must exist and be filled in with the user's real info (not the Jane Doe template).
- The dispatch payload sends `resume_dir` and `screenshot_dir` as absolute paths derived from the current repo location.

For v1 this is fine (single user, my machine). Anyone else wanting to run it would have to redo the OpenClaw config pointer and re-init their USER.md. That's captured as a known limitation and will get a proper install script before any open-source release.

---

## Install

Requirements:

- Python 3.9+
- [tectonic](https://tectonic-typesetting.github.io/) — LaTeX → PDF (`brew install tectonic` on macOS)
- [OpenClaw](https://openclaw.ai/) — agent runtime (`curl -fsSL https://openclaw.ai/install.sh | bash`)
- Bright Data "Scraping Browser" zone — residential Chrome via CDP
- API keys: Anthropic, Brave, spider.cloud (see [Configure](#configure))

```bash
git clone git@github.com:whoissegun/applyd.git
cd applyd
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

---

## Configure

### 1. `.env` at repo root

```bash
# required — discovery + enrichment + tailor
BRAVE_SEARCH_API_KEY=...
SPIDER_API_KEY=...
ANTHROPIC_API_KEY=...

# required — apply step (Bright Data residential Chrome via CDP)
BRIGHTDATA_CUSTOMER_ID=...
BRIGHTDATA_ZONE=...
BRIGHTDATA_ZONE_PASSWORD=...

# required — OpenClaw gateway auth (generated by openclaw's onboarding wizard;
# lives in ~/.openclaw/openclaw.json under gateway.auth.token)
OPENCLAW_TOKEN=...

# required — shared secret for the applyd callback server (any random string)
APPLYD_CALLBACK_TOKEN=...

# strongly recommended while testing — agent fills but never clicks submit
APPLYD_TEST_MODE=true

# optional
SERPER_API_KEY=...                        # search fallback
SEARCH_PROVIDER=brave                     # brave | serper (default brave)
BROAD_SEARCH_TTL_HOURS=6
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

### 4. OpenClaw — `~/.openclaw/openclaw.json`

Generate a default config via the OpenClaw onboarding wizard, then make sure these keys are present:

```json
{
  "gateway": {
    "mode": "local",
    "auth": { "mode": "token", "token": "<matches OPENCLAW_TOKEN in .env>" },
    "http": { "endpoints": { "chatCompletions": { "enabled": true } } }
  },
  "skills": {
    "load": {
      "extraDirs": [
        "/absolute/path/to/applyd/openclaw/skills"
      ]
    }
  }
}
```

The `skills.load.extraDirs` entry is what tells OpenClaw to load `applyd_apply` from this repo. Hot-reloads on edit. **Hardcoded absolute path** — this is the main portability constraint.

If the onboarding wizard crashed at the Feishu plugin step (Node 24 + `@larksuiteoapi/node-sdk` missing), the core config is usually fine — just run `openclaw config set` for anything missing.

### 5. OpenClaw workspace — `~/.openclaw/workspace/USER.md`

The apply agent reads this file as prose for every invocation. It's auto-injected — applyd doesn't send it. Covers everything the agent needs to fill a form:

- **Identity:** full legal name, preferred name, email, phone (with format hints), pronouns
- **Location:** current city, region, country; whether open to relocation
- **Links:** LinkedIn, GitHub, portfolio
- **Education:** school, degree, graduation month/year, GPA if worth including
- **Work authorization:** per-country (where you can work without sponsorship, where you need it). Be precise — the agent answers truthfully and refuses to fudge.
- **Demographics:** race, gender, veteran/disability status in the exact wording forms use. Include "decline to self-identify" as a valid answer.
- **Narrative hooks (optional but useful):** 3–5 bullets on "what I care about / what I'm curious about / the pattern of work I like." The agent uses these to anchor free-text answers like "Why us?" without slop.

A starter template lives at [`profile.example.md`](profile.example.md).

**Symlinks don't work** — OpenClaw rejects symlinks outside the workspace. Write the actual file.

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

# 5. Fire up the apply loop (two terminals)
#    terminal 1 — keep running
applyd callback
#    terminal 2 — one-shot per job
applyd apply-one
#   → reads pending_apply (tailored, not attempted, not gated)
#   → dispatches to OpenClaw's applyd_apply skill
#   → agent fills form, screenshots, POSTs result to the callback
#   → jobs.json gets updated
```

---

## CLI reference

| Command | Purpose |
|---|---|
| `applyd discover` | Pull from aggregators + broad search + user targets |
| `applyd enrich [--limit N] [--workers N] [--dry-run] [--retry-failed] [--source X]` | Fetch full JD text for jobs missing descriptions |
| `applyd tailor <job_id> [--no-compile] [--ignore-errors] [--model X] [--force]` | Generate tailored resume for a job (`--force` overrides gate check) |
| `applyd jobs [--level] [--specialty] [--location] [--remote] [--source] [--company] [--gated] [--no-gated] [--limit] [--format]` | Query the job store |
| `applyd resolve <company>` | Debug: company name → (ATS, slug) |
| `applyd callback` | Run the HTTP callback server (default 127.0.0.1:9000) |
| `applyd apply-one` | Dispatch the next pending job to OpenClaw |

---

## Rough cost expectations

At personal-use volume (~50 applications/month):

| Service | Monthly |
|---|---|
| Brave Search API | free ($5/mo credit covers ~1,000 queries) |
| spider.cloud | ~$0.50 one-time to enrich a 3k-job corpus; ~$0.10/mo incremental |
| Anthropic (tailor) | ~$0.50–$2 for ~50 tailors (prompt caching ~10× cheaper on re-reads) |
| Anthropic (apply, via OpenClaw) | ~$0.05–$0.15/apply (10–30k prompt tokens baseline + free-text output) |
| Bright Data | usage-based, ~$0.10–$0.30/session depending on form complexity |
| **Total** | **~$5–$15/mo** in steady state |

Heavily dependent on how many applies/day you run. Test-mode runs cost the same as real submits.

---

## Repo layout

```
applyd/
├── resume_base.tex            # YOUR base resume — the source of truth for tailoring
├── targets.json               # companies you specifically want tracked
├── profile.example.md         # template for ~/.openclaw/workspace/USER.md
├── requirements.txt           # pinned snapshot (pyproject.toml is the contract)
├── openclaw/
│   └── skills/
│       └── applyd-apply/
│           └── SKILL.md       # prose instructions for the apply agent
├── src/applyd/
│   ├── cli.py                 # argparse + main() (lean — dispatch only)
│   ├── config.py              # .env loader
│   ├── models.py              # Pydantic Job model
│   ├── store.py               # JSON file store + pending_apply filter
│   ├── filters.py             # --level / --specialty / --gated filters
│   ├── callback.py            # FastAPI receiver for the apply skill's result POST
│   ├── commands/              # one CLI subcommand per file
│   │   ├── discover.py
│   │   ├── enrich.py
│   │   ├── tailor.py
│   │   ├── jobs.py
│   │   ├── resolve.py
│   │   └── apply.py           # apply-one + callback runner
│   ├── apply/                 # legacy Bright Data CDP helper (kept as fallback)
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
    ├── broad_search_cache.json
    └── apply_screenshots/     # test-mode form screenshots, one per apply
```

Key file if you're reading the code cold:

- `src/applyd/cli.py` — see every subcommand at a glance
- `src/applyd/commands/apply.py` — dispatch payload (what the agent gets)
- `openclaw/skills/applyd-apply/SKILL.md` — exactly what the agent is told to do
- `src/applyd/discovery/routing.py` — ATS detection + the gated-domain blocklist
- `src/applyd/store.py` — the Job lifecycle & `pending_apply` filter

---

## Known limitations

- **Single-user, local execution.** Absolute paths to this repo hardcoded in `~/.openclaw/openclaw.json`. No multi-tenancy.
- **Apply step is test-mode only right now.** Need ≥5 visually-verified test-mode runs before flipping `APPLYD_TEST_MODE=false`.
- **Ambiguous company names** (e.g. "Mercury" the bank vs "Mercury Logistics Group") can resolve to the wrong ATS. Inspect `data/resolver_cache.json` after first run; edit by hand.
- **Stale aggregator URLs** (posting removed from the ATS between crawls) land in `fetch_tier="failed"`. Expected, not a bug.
- **Gated domains pre-filtered, not smart.** Workday / Oracle / Taleo / LinkedIn / Wellfound / etc. are skipped before tailor spend. Occasionally miscategorizes a direct-apply Workday — the agent has a runtime backstop but we leave money on the table for ~17% of discovered jobs.
- **SerpAPI deliberately unsupported** as default search provider (active Google DMCA lawsuit, Dec 2025). Brave is default; Serper kept only as a kept-swappable fallback.

---

## Credits

- Resume template: [Jake's Resume](https://github.com/jakegut/resume) by Jake Gutierrez (MIT).
- Discovery inspiration: [SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions) maintainers.
- Agent runtime: [OpenClaw](https://openclaw.ai/) by Peter Steinberger.
