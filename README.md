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
| SmartRecruiters | Supported | Routed to human review without model spend |
| Workday and other ATSes | Excluded by default | Manual |

SmartRecruiters is manual-only because repeated pilots across independent
employers produced no confirmed submissions. It remains searchable and rankable
so suitable jobs are not lost.

## Requirements

- Python 3.11 or newer
- Google Chrome, with Playwright Chromium available as a fallback
- [Tectonic](https://tectonic-typesetting.github.io/) for LaTeX compilation
- Poppler (`pdfinfo`) for page-count validation
- An [OpenRouter](https://openrouter.ai/) API key
- Optional: Brave Search or Serper credentials for broad/company discovery
- Optional: Bright Data Scraping Browser credentials for configured fallbacks

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

Create a local environment file:

```dotenv
OPENROUTER_API_KEY=your_openrouter_key

# Optional discovery provider
BRAVE_SEARCH_API_KEY=your_brave_key
SEARCH_PROVIDER=brave

# Safety defaults
APPLYD_TEST_MODE=true
APPLYD_BROWSER_HEADLESS=true
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

Local Chrome is the default application provider. Lever starts with Bright Data
for real batch runs because the pilot repeatedly encountered CAPTCHA gates.
Other supported ATSes retry through Bright Data only after an explicit CAPTCHA,
when `--captcha-fallback brightdata` is enabled.

Bright Data is optional and is not used for discovery, retrieval, extraction,
matching, or tailoring. To configure it:

```dotenv
BRIGHTDATA_CUSTOMER_ID=your_customer_id
BRIGHTDATA_ZONE=your_zone
BRIGHTDATA_ZONE_PASSWORD=your_zone_password
BRIGHTDATA_COUNTRY=ca
```

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
| Resume tailoring | $0.3193 | $0.0160 |
| Application agent | $0.6764 | $0.0338 |
| **Combined** | **$0.9957** | **$0.0498** |

This is a sample, not a price guarantee. Form length, retries, cache behavior,
model pricing, and output size change the result. The figures exclude Bright
Data charges and any interrupted request that could not be persisted.

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
| `APPLYD_APPLY_MAX_TURNS` | No | Application model-turn ceiling; defaults to `25` |
| `APPLYD_APPLY_MAX_SECONDS` | No | Secondary application wall-clock ceiling |
| `APPLYD_IMAP_USER` | No | Mailbox for supported verification-code retrieval |
| `APPLYD_IMAP_PASSWORD` | No | Mailbox app password |
| `BRIGHTDATA_*` | No | Optional remote browser and CAPTCHA fallback |

Keep credentials in `.env`; never commit them.

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
- SmartRecruiters is currently manual-only in batch mode.
- Lever can remain CAPTCHA-gated even with the optional remote browser.
- Workday is intentionally outside the automated apply path.
- The LaTeX importer currently targets Jake-style resume structure.
- The project does not yet include a dashboard for the review queue.

## License

This repository does not currently include a license file. Until a license is
added, copyright remains with the repository owner and reuse rights are not
granted automatically.
