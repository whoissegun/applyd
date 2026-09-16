# applyd

Local-first, CLI-first job discovery and application automation.

applyd discovers jobs, retrieves and structures descriptions, evaluates them
against a candidate profile, ranks them locally, tailors a resume, and operates
supported application forms. Runtime state lives in a SQLite file on your
laptop. There is no database server, hosted worker, or Supabase dependency in
the default path.

> [!CAUTION]
> applyd can submit real job applications. Test mode is the default. Review
> your profile, resume, generated documents, and target jobs before running
> with `--test-mode false`. You are responsible for the accuracy of submitted
> information and for complying with each website's terms and policies.

## Start here

- [Complete setup guide](setup.md) — install, configure OpenRouter, initialize
  SQLite and Chrome, run a safe test, and troubleshoot the result.
- [Candidate profile questionnaire](PROFILE_QUESTIONS.md) — the questions and
  coding-agent prompt used to build a grounded `profile.json`.
- [`profile.example.json`](profile.example.json) — profile schema and safe
  defaults.
- [`resume.example.json`](resume.example.json) — canonical factual resume
  structure.

If this is your first install, follow `setup.md` rather than piecing the setup
together from the command reference below.

## Highlights

- **Local state:** jobs, extracted facts, decisions, scores, application state,
  costs, and redacted traces are stored in `data/applyd.sqlite3`.
- **CLI-first pipeline:** every stage can be run, inspected, and retried
  independently.
- **Low-cost model usage:** Kimi K2.6 runs through OpenRouter only where natural
  language interpretation or generation is useful.
- **Local matchmaking:** embeddings and deterministic reranking run on-device.
- **Grounded tailoring:** Kimi returns structured edits; Python validates source
  IDs and renders LaTeX. The model never writes LaTeX directly.
- **Bounded application agent:** navigation and uploads are runner-controlled,
  model turns are capped, and successful submission requires later confirmation.
- **Persistent Chrome:** local browser sessions can preserve cookies and login
  state between runs.
- **Human review:** missing consequential facts and unsupported flows stop safely
  instead of being guessed.

## How it works

```text
discover -> enrich/extract -> evaluate -> deduplicate -> match -> verify -> tailor -> apply
   HTTP      ATS/HTTP/Chrome     rules          rules       local      HTTP     Kimi     Kimi
                 + Kimi                                  vectors             + LaTeX  + Chrome
                                          SQLite
```

| Stage | Responsibility | Typical cost |
| --- | --- | ---: |
| Discover | Pull aggregator, ATS, company, and search results | Free, except optional search API |
| Enrich | Retrieve descriptions through ATS APIs, HTTP, then local Playwright | Free |
| Extract | Convert descriptions into evidence-backed facts | OpenRouter |
| Evaluate | Apply work-authorization and preference policy | Free |
| Deduplicate | Group cross-source copies of the same posting | Free |
| Match | Local embeddings plus deterministic reranking | Free |
| Verify | Check whether selected postings are still live | Free |
| Tailor | Produce structured resume edits and compile a PDF | OpenRouter |
| Apply | Fill a supported form with a bounded browser tool loop | OpenRouter; optional browser provider |

ATS adapters own transport normalization. Extracted consequential facts require
verbatim evidence, and unsupported blocking conclusions are reset to neutral.
Unknown eligibility facts remain eligible by default unless the profile requests
review.

## ATS support

| ATS | Discovery and retrieval | Batch application behavior |
| --- | --- | --- |
| Ashby | Supported | Local Chrome |
| Greenhouse | Supported | Local Chrome; optional CAPTCHA fallback |
| Lever | Supported | Bright Data by default for real batch runs |
| Workable | Supported | Local Chrome; limited pilot coverage |
| SmartRecruiters | Excluded by default | Manual-only research with explicit opt-in |
| Workday and other ATSes | Excluded by default | Manual |

SmartRecruiters is a known difficult automation platform: repeated pilots across
independent employers produced no confirmed submissions. applyd therefore does
not ingest, rank, or batch-select SmartRecruiters jobs by default. Use
`applyd discover --include-unsupported-ats` only when you intentionally want
those postings in the local catalog for manual research.

## Requirements

- Python 3.11 or newer
- Google Chrome, with Playwright Chromium available as a fallback
- [Tectonic](https://tectonic-typesetting.github.io/) for LaTeX compilation
- Poppler (`pdfinfo`) for page-count validation
- An [OpenRouter](https://openrouter.ai/) API key
- Optional: Brave Search or Serper credentials for broad/company discovery
- Optional: Bright Data Scraping Browser credentials for configured fallbacks

See [setup.md](setup.md) for macOS, Linux, and Windows environment instructions,
current provider links, browser-profile setup, expected costs, and a first-run
checklist.

On macOS, install the system dependencies with:

```bash
brew install tectonic poppler
```

## Quick start

```bash
git clone https://github.com/whoissegun/applyd.git
cd applyd

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
playwright install chromium
```

Create a local environment file and add your OpenRouter key:

```bash
cp .env.example .env
```

Create local candidate files and initialize SQLite:

```bash
cp profile.example.json profile.json
cp resume.example.json resume.json

# Replace the fictional values before continuing.
applyd init
```

`profile.json`, `resume.json`, `.env`, `data/`, and generated `out/` files are
ignored by Git.

## Candidate profile and resume

`profile.json` is the authoritative source for identity, contact information,
work authorization, education, preferences, background defaults, and writing
policy. Do not put facts in the profile that you would not authorize applyd to
submit.

Role targeting and role exclusion are separate. `matchmaking.target_role_families`
boosts preferred families but is not a strict allowlist. To prevent a grounded
role family from entering the application funnel, add it to the personal
profile policy:

```json
{
  "preferences": {
    "excluded_role_families": ["data"],
    "exclude_title_patterns": ["\\bstrategic analyst\\b"]
  },
  "matchmaking": {
    "target_role_families": ["software_engineering", "machine_learning"]
  }
}
```

Supported role-family values are `software_engineering`, `machine_learning`,
`data`, `security`, `product`, `design`, `sales`, `operations`, and `other`.
Role-family exclusions use evidence-grounded extracted facts; title patterns
are case-insensitive regular expressions for narrower personal rules. After
changing either setting, rerun `applyd evaluate` and `applyd match`.

Use [PROFILE_QUESTIONS.md](PROFILE_QUESTIONS.md) to build it manually or with
Codex, Claude Code, Cursor, or another repository-aware coding agent. That guide
separates factual answers from preferences, provides a reusable agent prompt,
and includes a final legal/identity accuracy checklist.

`resume.json` is the canonical source for tailoring. Each experience, project,
and bullet has a stable source ID. Tailoring may select, shorten, combine, style,
and persuasively rephrase those facts, but may not invent employers,
technologies, credentials, dates, metrics, or historical events.

To convert a Jake-style LaTeX resume deterministically:

```bash
applyd import-resume resume_base.tex
```

The importer makes no model call. It copies the resume's structure and assigns
stable IDs, while contact details come from `profile.json`.

## Run the pipeline

Start with a small, non-submitting run:

```bash
# SimplifyJobs ingestion works without a search key. --no-broad disables
# optional broad web discovery.
applyd discover --limit 1000 --no-broad

# Retrieve descriptions and extract grounded structured facts in parallel.
applyd enrich --workers 8 --batch-size 5

# Apply deterministic candidate policy, group duplicates, and rank locally.
applyd evaluate --profile profile.json --show-reasons
applyd dedupe
applyd match --top 50

# Check selected postings immediately before application.
applyd verify-live --top 20

# Generate one tailored PDF, then fill without submitting.
applyd tailor <job_id>
applyd apply <job_id>
```

Inspect the result and trace:

```bash
applyd trace <job_id>
applyd profile-gaps
```

Only after reviewing the setup, permit a real submission explicitly:

```bash
applyd apply <job_id> --test-mode false
```

For a bounded serial batch:

```bash
applyd apply-batch --top 20 --test-mode false
```

Batch runs limit jobs per ATS, stop an ATS after repeated platform failures,
avoid previously attempted cross-source duplicates, and write an atomic JSON
report under `data/batches/`.

### Useful commands

```bash
applyd jobs --company Stripe --limit 20
applyd jobs --format json --limit 50
applyd enrich --no-extract              # retrieval only; no model calls
applyd enrich --no-browser              # ATS APIs and HTTP only
applyd enrich --classify-backfill       # extract existing descriptions
applyd match --format json              # inspect score components
applyd match --rebuild-embeddings       # rebuild the local vector cache
applyd tailor <job_id> --force          # bypass missing/ineligible evaluation
applyd trace <job_id> --errors-only
applyd trace <job_id> --compare
```

Run `applyd <command> --help` for the complete options.

## Browser behavior

The default apply profile is `data/browser/apply-profile`; retrieval uses the
separate `data/browser/retrieval-profile`. Real local submissions open visible
Chrome unless `APPLYD_BROWSER_HEADLESS` is explicitly set.

Initialize the dedicated application profile and sign in once with:

```bash
applyd browser-login
```

applyd does not attach to an already-open everyday Chrome profile and local mode
does not require a CDP URL. It launches a dedicated persistent profile directly
through Playwright. Do not set `APPLYD_BROWSER_PROFILE` to Chrome's default
`User Data` directory; use a separate directory as described in
[setup.md](setup.md#7-set-up-the-dedicated-chrome-profile).

Local Chrome is the default application provider. Lever starts with Bright Data
for real batch runs because the pilot repeatedly encountered CAPTCHA gates.
Other supported ATSes retry through Bright Data only after an explicit CAPTCHA,
when `--captcha-fallback brightdata` is enabled.

Bright Data is optional and is not used for discovery, retrieval, extraction,
matching, or tailoring. To configure it:

```dotenv
# Paste the full endpoint shown in the Bright Data Browser API zone:
BRIGHTDATA_CDP_URL=wss://your-complete-endpoint

# Or provide its components instead:
BRIGHTDATA_CUSTOMER_ID=your_customer_id
BRIGHTDATA_ZONE=your_zone
BRIGHTDATA_ZONE_PASSWORD=your_zone_password
BRIGHTDATA_COUNTRY=ca
```

The optional Bright Data CDP endpoint comes from a Browser API / Scraping
Browser zone in the Bright Data control panel. It is unrelated to the local
Chrome profile. See the [CDP instructions](setup.md#9-optional-bright-data-and-its-cdp-url).

A provider-reported CAPTCHA solve is not submission evidence. applyd records
`applied` only after a later turn observes an ATS confirmation marker,
confirmation navigation, or a closed submitted form.

## Safety model

- Test mode is the default; real submission requires `--test-mode false`.
- The model cannot choose the navigation URL or upload an arbitrary file.
- Required questions are inspected before the agent starts filling each form
  step.
- Missing identity, legal, education, employment, compensation, demographic,
  or preference facts enter human review.
- Optional cover letters and optional free text are skipped by default.
- Required cover letters are generated only from grounded candidate facts.
- Typed values and snapshot values are redacted from persisted traces.
- Application loops default to 25 model turns and a secondary wall-clock limit.
- Costs above $0.10 per application are flagged; hard cost limits are optional.

No automation can guarantee that every website behaves consistently. Review the
human queue and application history regularly.

## Cost

HTTP retrieval, SQLite, policy evaluation, local embeddings, LaTeX rendering,
and local Chrome do not incur model fees.

In the latest 20-job live validation batch, recorded OpenRouter usage was:

| Component | Total | Average per selected job |
| --- | ---: | ---: |
| Resume tailoring | $0.1956 | $0.0098 |
| Application agent | $0.4315 | $0.0216 |
| **Combined** | **$0.6271** | **$0.0314** |

This is a sample, not a price guarantee. Form length, retries, cache behavior,
model pricing, and output size change the result. The figures exclude Bright
Data charges and any interrupted request that could not be persisted. In this
batch, 17 jobs required tailoring while three SmartRecruiters jobs went directly
to zero-cost manual review.

A separate fresh extraction run processed 95 jobs for $0.2262, or roughly
$0.00238 per job in that sample. As of September 10, 2026,
[OpenRouter lists Kimi K2.6](https://openrouter.ai/moonshotai/kimi-k2.6) at a
cheapest-endpoint price of $0.58/M input tokens, $3.40/M output tokens, and
$0.058/M cached-input tokens. Always check the live model page before a large
run. [setup.md](setup.md#planning-your-budget) translates these samples into a
starter budget and documents the available hard cost caps.

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | Yes | Semantic extraction, tailoring, and application agent |
| `BRAVE_SEARCH_API_KEY` | No | Brave-based company and broad discovery |
| `SERPER_API_KEY` | No | Alternative search provider |
| `SEARCH_PROVIDER` | No | `brave` or `serper`; defaults to `brave` |
| `APPLYD_TEST_MODE` | No | Default submit policy; defaults to `true` |
| `APPLYD_BROWSER_HEADLESS` | No | Force headed or headless local browser behavior |
| `APPLYD_BROWSER_PROFILE` | No | Persistent application Chrome profile path |
| `APPLYD_RETRIEVAL_PROFILE` | No | Persistent retrieval Chrome profile path |
| `APPLYD_TAILOR_CALL_MAX_SECONDS` | No | Whole-request tailoring deadline; defaults to `180` |
| `APPLYD_APPLY_MAX_TURNS` | No | Application model-turn ceiling; defaults to `25` |
| `APPLYD_APPLY_MAX_SECONDS` | No | Secondary application wall-clock ceiling |
| `APPLYD_IMAP_USER` | No | Mailbox for supported verification-code retrieval |
| `APPLYD_IMAP_PASSWORD` | No | Mailbox app password |
| `BRIGHTDATA_CDP_URL` | No | Full optional Bright Data Browser API WebSocket endpoint |
| `BRIGHTDATA_*` | No | Alternative Bright Data endpoint components and CAPTCHA fallback |

Keep credentials in `.env`; never commit them.
For Greenhouse email-code verification, see
[Optional: email verification codes](setup.md#8-optional-email-verification-codes),
including Gmail two-step verification and App Password setup.

## Project layout

```text
src/applyd/
├── apply/          # bounded browser loop, tools, prompts, browser providers
├── commands/       # CLI stage implementations
├── discovery/      # aggregators, ATS adapters, routing, search providers
├── enrichment/     # retrieval and evidence-backed semantic extraction
├── tailor/         # structured edit plan, deterministic LaTeX, PDF compile
├── eligibility.py  # deterministic candidate policy
├── deduplication.py
├── matching.py     # local embeddings and reranking
└── local_store.py  # SQLite schema and repositories

tests/              # unit and browser-tool regression tests
scripts/            # import, benchmark, and ATS test utilities
setup.md             # complete installation and first-run guide
PROFILE_QUESTIONS.md # reusable candidate interview and agent prompt
```

## Development

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/python -m applyd.cli --help
```

When changing resume rendering, compile and visually inspect a real PDF. When
changing browser behavior, add a regression test for the observed ATS pattern
and start in test mode.

Contributions should keep the default path local-first, preserve deterministic
boundaries, and avoid adding paid infrastructure where a local or ordinary HTTP
path is sufficient.

## Known limitations

- ATS markup and anti-automation behavior change without notice.
- SmartRecruiters is excluded from default discovery, ranking, and batch
  selection because repeated automation pilots produced no confirmed
  submissions. It remains available through explicit manual-research opt-in.
- Lever can remain CAPTCHA-gated even with the optional remote browser.
- Workday is intentionally outside the automated apply path.
- The LaTeX importer currently targets Jake-style resume structure.
- The project does not yet include a dashboard for the review queue.

## License

This repository does not currently include a license file. Until a license is
added, copyright remains with the repository owner and reuse rights are not
granted automatically.
