# applyd

Local-first, CLI-first job discovery and application automation. State stays in
SQLite on your laptop. Hosted Kimi K2.6 is used only for job interpretation,
resume tailoring, and application-form operation.

The default apply browser is a persistent local Chrome profile. Bright Data is
optional compatibility—not a requirement. Supabase and hosted workers are not
part of the local runtime.

```text
discover -> enrich + extract -> evaluate -> tailor -> apply
   HTTP       ATS/HTTP/Chrome      rules       Kimi     Kimi + Chrome
                 + Kimi                       + LaTeX
                           SQLite
```

- ATS adapters normalize platform responses into one `Job` model.
- Retrieval tries an ATS API, ordinary HTTP, then local Playwright.
- Kimi extracts evidence-backed semantic facts.
- Deterministic profile rules evaluate eligibility. Unknowns are eligible by
  default so volume is not silently lost.
- Kimi returns a structured resume edit plan; Python owns LaTeX and PDF output.
- The apply agent can navigate only to the selected URL and upload only the
  selected PDF. `applied` requires confirmed submit evidence.

## Setup

Requirements: Python 3.11+, Chrome or Playwright Chromium, and
[`tectonic`](https://tectonic-typesetting.github.io/) for PDFs.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
brew install tectonic poppler   # macOS
```

Create `.env`:

```dotenv
OPENROUTER_API_KEY=...
BRAVE_SEARCH_API_KEY=...       # optional: broad discovery and resolution
APPLYD_TEST_MODE=true
APPLYD_BROWSER_HEADLESS=true
```

```bash
cp profile.example.json profile.json
cp resume.example.json resume.json
applyd init
```

To import the former JSON catalog: `applyd init --import-legacy data/jobs.json`.

## Usage

```bash
applyd discover --limit 1000
applyd enrich --workers 8 --batch-size 5
applyd evaluate --profile profile.json --show-reasons
applyd dedupe
applyd match --top 50
applyd verify-live --top 20
applyd jobs --company Stripe --limit 20
applyd tailor <job_id>
applyd apply <job_id>                         # fills only; never submits
applyd apply <job_id> --test-mode false       # permits a real submission
applyd apply-batch --top 20 --test-mode false # serial, bounded real pilot
applyd profile-gaps                           # unresolved required facts
applyd trace <job_id>                         # redacted latest-attempt timeline
applyd trace <job_id> --compare               # compare providers, turns, and cost
```

Discovery keeps only Greenhouse, Lever, Ashby, Workable, and SmartRecruiters by
default because those are the application platforms the agent supports. Use
`--include-unsupported-ats` only for retrieval research; unsupported platforms
never enter the normal apply queue.

Useful variants:

```bash
applyd enrich --classify-backfill   # extract existing descriptions
applyd enrich --no-browser          # ATS + HTTP only
applyd enrich --no-extract          # retrieval only; no model cost
applyd match --format json          # inspect full score components
applyd match --rebuild-embeddings   # discard the local vector cache
applyd tailor <job_id> --force      # bypass absent/ineligible evaluation
```

Matchmaking runs locally with a small ONNX embedding model. The first run
downloads the model and embeds all eligible jobs; later runs reuse unchanged
vectors. KNN similarity is reranked with explicit role, seniority, technology,
recency, work-authorization, and ATS-readiness components stored in SQLite.

`profile.json` contains identity, legal facts, and preferences. Legal answers
must be exact; motivation/opinion answers may be composed from grounded context.
Before changing each visible form step, the apply agent preflights every required
question. Consequential unknowns are aggregated in SQLite for `profile-gaps` and
the form is left for review; optional fields remain blank. Required referral
source questions use the profile's authorized safe fallback list.
`resume.json` is the canonical tailoring source. Generate it locally and
deterministically from a Jake-style LaTeX resume with:

```bash
applyd import-resume resume_base.tex
```

Use `--profile another-profile.json` or `--output another-resume.json` when
needed. The importer copies education, experience, projects, skills, dates,
metrics, and bullets from TeX, assigns stable source IDs, and uses the profile
for contact details. It makes no model call and costs nothing.

The generated policy protects the newest experience and preserves the source
experience order. Tailoring can remove less-relevant older content to fit one
page, but it cannot silently drop the newest role or reorder employment dates.

Tailoring may reorder, combine, shorten, style, and persuasively rephrase source
bullets. It may not add employers, technologies, credentials, dates, metrics,
or past events. The renderer validates source IDs, length, compilation, and
one-page output without a second LLM call.

## Browser and cost

The persistent apply profile is `data/browser/apply-profile`; retrieval uses
`data/browser/retrieval-profile`. Set `APPLYD_BROWSER_HEADLESS=false` to watch.
Browser routing defaults to local Chrome, except Lever applications start with
Bright Data because the pilot consistently encountered Lever CAPTCHA gates.
SmartRecruiters jobs remain discoverable and rankable but `apply-batch` routes
them to human review without tailoring or model calls: repeated pilots produced
no confirmed submissions across independent employers.
Override explicitly with `applyd apply <job_id> --browser local` or
`--browser brightdata`.
On Bright Data real submits, applyd invokes the provider's CAPTCHA CDP solver
and still requires an ATS success marker, navigation, or closed form before it
records `applied`; a provider-reported solve alone is not submission evidence.

Apply traces store refs, action outcomes, turn counts, and costs in SQLite.
Typed values and snapshot values are redacted. Use `--errors-only`, `--all`, or
`--format json` for debugging and eval comparisons.

`apply-batch` runs serially, limits attempts per ATS, stops an ATS
after repeated platform failures, caps per-application and total model cost, and
writes an atomic JSON report. `--captcha-fallback brightdata` retries only an
explicit CAPTCHA once on non-Lever ATSes; Lever starts on Bright Data. Other
failures stay local or enter human review. The default apply ceiling is 25 model
turns; cost does not stop a run, but attempts above $0.10 are flagged in logs and
batch reports. Wall-clock timeout remains a secondary hung-browser safeguard.

HTTP, ATS APIs, SQLite, rules, LaTeX, and local Chrome are free. Measured Kimi
K2.6 OpenRouter samples:

- job extraction: about $0.0013/job in the five-job benchmark;
- resume tailoring: about $0.005 for the tested resume;
- application loop: $0.0054 on the local integration form, and about
  $0.0087/application in the earlier two-application benchmark.

Costs vary with input and form length. Models remain CLI options so a later
local model can replace Kimi without changing pipeline boundaries.

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

The test fixture at `tests/fixtures/application.html` supports a live hosted-
Kimi apply-loop check in non-submitting mode.
