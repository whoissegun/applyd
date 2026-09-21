# applyd setup guide

This guide takes a new user from a fresh clone to a safe test application.
Complete it in order. Real submission is disabled by default.

> [!CAUTION]
> applyd can submit real job applications when explicitly started with
> `--test-mode false`. You are responsible for the accuracy of your profile and
> resume and for complying with the terms and policies of each job site. Begin
> with test mode and inspect the generated resume and application trace.

## What stays local and what does not

The SQLite database, candidate profile, resume, browser profile, downloaded
embedding model, application traces, and generated PDFs stay on your computer.
They live in ignored files under `data/`, `out/`, `profile.json`, and
`resume.json`.

Job descriptions and the candidate context needed for extraction, tailoring,
and form interpretation are sent to Kimi K2.6 through OpenRouter. If you enable
Bright Data, pages handled by that fallback pass through Bright Data's Browser
API. Do not use either service if its data handling is unacceptable to you.

## 1. Install prerequisites

You need:

- Git
- Python 3.11 or newer
- Google Chrome
- [Tectonic](https://tectonic-typesetting.github.io/) to compile LaTeX resumes
- Poppler's `pdfinfo` to verify page counts
- an [OpenRouter](https://openrouter.ai/) account and API key

On macOS with Homebrew:

```bash
brew install python git tectonic poppler
```

On Ubuntu or Debian, install Python, Git, and Poppler through `apt`. Install
Tectonic using its [official installation instructions](https://tectonic-typesetting.github.io/book/latest/installation/).

## 2. Clone and install applyd

```bash
git clone https://github.com/whoissegun/applyd.git
cd applyd

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
playwright install chromium
```

On Windows PowerShell, activate the environment with:

```powershell
.venv\Scripts\Activate.ps1
```

Confirm the CLI is installed:

```bash
applyd --help
```

## 3. Create an OpenRouter key

1. Create or sign in to an [OpenRouter account](https://openrouter.ai/).
2. Add a small credit balance.
3. Create a key in [OpenRouter Keys](https://openrouter.ai/settings/keys).
4. Copy the environment template and put the key in `.env`:

```bash
cp .env.example .env
```

```dotenv
OPENROUTER_API_KEY=sk-or-v1-your-real-key
```

Never commit `.env` or paste API keys into an AI chat. The repository ignores
`.env` automatically.

applyd currently defaults to `moonshotai/kimi-k2.6`. As of September 10, 2026,
[OpenRouter lists Kimi K2.6](https://openrouter.ai/moonshotai/kimi-k2.6) at
$0.58 per million input tokens, $3.40 per million output tokens, and $0.058 per
million cached-input tokens at its cheapest listed endpoint. Provider routing
and prices can change, so check the model page before funding a large run.

### Planning your budget

Measured project runs give more useful guidance than token prices alone:

| Work | Observed cost | Planning estimate |
| --- | ---: | ---: |
| Extract 95 newly retrieved jobs | $0.2262 | about $2.38 per 1,000 at the same mix |
| Tailor and apply in a 20-job validation | $0.6271 | $0.0314 per selected job |
| Long or retry-heavy application | sometimes above $0.10 | flagged automatically |

These are samples, not guarantees. Job-description length, form length,
provider routing, cache hits, retries, and model pricing all change the result.
Bright Data and optional search APIs are excluded. A $5 balance is comfortable
for setup, extraction experiments, and a small application pilot; do not fund a
large batch until your own traces establish a typical cost.

You can add hard limits:

```bash
applyd apply <job_id> --max-cost-usd 0.10
applyd apply-batch --top 20 --max-total-apply-cost-usd 1.00
```

## 4. Build `profile.json`

The profile is the authoritative source for identity, legal status, education,
preferences, demographics, and application defaults. Start with:

```bash
cp profile.example.json profile.json
```

Then use [PROFILE_QUESTIONS.md](PROFILE_QUESTIONS.md). It contains a complete
questionnaire and a prompt you can give to Codex, Claude Code, Cursor, or
another coding agent. Attach your resume and ask the agent to interview you,
then update `profile.json` against `profile.example.json`.

Review every value yourself, especially:

- legal name and contact information;
- work authorization and sponsorship for each country;
- citizenship, security-clearance, and export-control answers;
- education, graduation date, GPA, employers, dates, and metrics;
- demographic answers and whether you prefer to disclose them;
- relocation, onsite, travel, salary, and role preferences.

Leave an unknown consequential fact absent so the application enters review.
Do not ask the agent to guess it. Do not put API keys, passwords, government ID
numbers, or financial information in `profile.json`.

### Exclude role families or titles

`matchmaking.target_role_families` controls ranking preferences; it is not a
strict allowlist. Use the personal deterministic policy when a grounded role
family must never be selected:

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

Valid family values are `software_engineering`, `machine_learning`, `data`,
`security`, `product`, `design`, `sales`, `operations`, and `other`.
`exclude_title_patterns` accepts case-insensitive regular expressions and is
useful when only particular titles should be excluded. These choices stay in
the ignored `profile.json`; they are not hardcoded into applyd. Rerun the
following commands after a preference change:

```bash
applyd evaluate --profile profile.json --show-reasons
applyd match --top 50
```

## 5. Build `resume.json`

`resume.json` is the factual source used during tailoring. If your resume is a
Jake-style LaTeX file:

```bash
applyd import-resume path/to/base_resume.tex
```

The importer writes `resume.json` with stable IDs for experiences, projects,
and bullets. It does not call an LLM. Contact details come from `profile.json`.

If you only have PDF or DOCX, give the document, `resume.example.json`, and this
instruction to your coding agent:

```text
Convert my attached resume into resume.json using resume.example.json as the
schema. Preserve every factual employer, title, date, project, technology, and
metric exactly. Assign stable kebab-case IDs to every experience, project, and
bullet. Do not improve, infer, or invent facts. Show me any ambiguous source
text before writing the file. Validate the final JSON.
```

Compare the result to your source resume. Tailoring can rephrase grounded
bullets, but it must never introduce a new employer, technology, credential,
date, metric, or historical event.

## 6. Initialize SQLite

```bash
applyd init
```

This creates `data/applyd.sqlite3` and imports `profile.json`. SQLite runs
inside the process; there is no database server, account, or port to configure.

Re-run `applyd init` after changing `profile.json` so the stored profile is
current. The `evaluate` command also persists the supplied profile.

## 7. Set up the dedicated Chrome profile

applyd launches Chrome with a dedicated persistent user-data directory. The
default is:

```text
data/browser/apply-profile
```

Cookies and sign-in state written there survive later runs. Initialize it with:

```bash
applyd browser-login
```

Chrome opens visibly. Sign in only to sites you want applyd to access, return
to the terminal, and press Enter. You can start on a particular page:

```bash
applyd browser-login --url https://accounts.google.com/
```

To use another dedicated directory:

```dotenv
APPLYD_BROWSER_PROFILE=/absolute/path/to/applyd-chrome-profile
```

Do not point this at Chrome's everyday `User Data` directory and do not copy an
active Chrome profile. Chrome locks a user-data directory while it is running,
and [Playwright explicitly warns that automating the default Chrome profile is
unsupported](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context).
Use applyd's separate directory and sign in once.

### Do I need a local CDP URL?

No. Local mode calls Playwright's `launch_persistent_context` and starts Chrome
itself. There is no local CDP URL to find, no remote-debugging port to enable,
and no already-open Chrome window to attach.

Set this while testing so you can watch the browser:

```dotenv
APPLYD_BROWSER_HEADLESS=false
```

## 8. Optional: email verification codes

Some Greenhouse applications email a one-time code after submission. applyd can
read that code over IMAP and enter it in the same browser session.

For Gmail, enable two-step verification and create a Google App Password. Use
the app password, never the normal account password:

```dotenv
APPLYD_IMAP_USER=you@gmail.com
APPLYD_IMAP_PASSWORD=your-16-character-app-password
APPLYD_IMAP_HOST=imap.gmail.com
APPLYD_IMAP_PORT=993
```

The mailbox should match the email in `profile.json`. Without these variables,
email-verification applications stop for human review.

## 9. Optional: Bright Data and its CDP URL

Bright Data is not needed for discovery, retrieval, extraction, matching,
tailoring, or ordinary local applications. In real batches, applyd starts Lever
through Bright Data because repeated local pilots encountered CAPTCHA gates.
Other supported ATSes use it only as a CAPTCHA fallback.

To enable it:

1. Create a Browser API / Scraping Browser zone in the
   [Bright Data control panel](https://brightdata.com/cp/zones).
2. Open the zone's connection or Playwright instructions.
3. Copy the WebSocket CDP endpoint. It resembles
   `wss://brd-customer-...-zone-...:PASSWORD@brd.superproxy.io:9222`.
4. Paste it into `.env`:

```dotenv
BRIGHTDATA_CDP_URL=wss://your-complete-endpoint
```

Choose the exit country in the zone or copied endpoint. Component variables are
ignored when `BRIGHTDATA_CDP_URL` is present.

Bright Data's [official Browser API example](https://docs.brightdata.com/api-reference/SDK#connect-to-scraping-browser)
uses this endpoint with Playwright's `connect_over_cdp`. Treat it as a secret.

Alternatively, provide the components and let applyd build the URL:

```dotenv
BRIGHTDATA_CUSTOMER_ID=your_customer_id
BRIGHTDATA_ZONE=your_zone_name
BRIGHTDATA_ZONE_PASSWORD=your_zone_password
BRIGHTDATA_COUNTRY=ca
```

Bright Data pricing varies by plan and traffic. Check its dashboard, set a
spending limit there, and remember that its charges are not included in
applyd's OpenRouter cost totals. A failed CAPTCHA solve is recorded for review,
not as a successful application.

## 10. Optional: broader job discovery

SimplifyJobs ingestion works without another API key:

```bash
applyd discover --limit 1000 --no-broad
```

SmartRecruiters is intentionally excluded from default discovery, ranking, and
batch selection because repeated pilots produced no confirmed automated
submissions. To retain those postings strictly for manual research, opt in:

```bash
applyd discover --limit 1000 --include-unsupported-ats
```

Rerun `applyd match` after upgrading so previously stored SmartRecruiters rows
are removed from the active ranking table.

For broader web discovery, configure Brave Search or Serper in `.env`:

```dotenv
SEARCH_PROVIDER=brave
BRAVE_SEARCH_API_KEY=your_key
```

Edit `targets.json` to add companies you care about. ATS adapters normalize
supported ATS responses; the LLM does not normalize transport formats.

## 11. Run the pipeline safely

Begin with a small dataset:

```bash
applyd discover --limit 1000 --no-broad
applyd enrich --workers 8 --batch-size 5
applyd evaluate --profile profile.json --show-reasons
applyd dedupe
applyd match --top 50
applyd verify-live --top 20
```

The first `match` downloads a small local embedding model into `data/models/`.
Matching after that is local and does not call OpenRouter.

Inspect candidates:

```bash
applyd jobs --limit 20
applyd match --top 20 --format json
```

Tailor one resume and run a non-submitting application:

```bash
applyd tailor <job_id>
applyd apply <job_id>
```

Inspect the PDF under `out/`, watch the browser, and read the redacted trace:

```bash
applyd trace <job_id>
applyd profile-gaps
```

Only after that should you explicitly allow a real submission:

```bash
applyd apply <job_id> --test-mode false
```

For a serial batch with a report:

```bash
applyd apply-batch \
  --top 5 \
  --test-mode false \
  --captcha-fallback brightdata \
  --report data/batches/pilot-5.json
```

Start with five. Review every outcome before increasing the batch size.

## 12. View application history

From your project directory after completing installation:

```bash
source .venv/bin/activate
applyd dashboard
```

On Windows, use `.venv\Scripts\Activate.ps1` instead of `source`.

This opens a local browser dashboard with applied jobs, confirmation dates,
resume PDFs, and the review queue. The server stays in the terminal until you
press Ctrl+C. Use `--no-open` to print the URL without opening a browser, or
`--port 8766` if the default port is busy. It needs no extra installation or
OpenRouter key and does not start applications. Click Refresh history after
running CLI work. Dates display in your browser's timezone.

Future application attempts save their upload PDF under
`data/application-resumes/`; keep that folder when backing up your database.
For older attempts, the dashboard labels the linked file as historical because
the exact original PDF was not archived. It will not claim an unavailable file
is the resume that was submitted.

## 13. Use a coding agent to help

Codex, Claude Code, Cursor, or a similar repository-aware agent can guide the
setup and diagnose local errors. From the repository root, use this prompt:

```text
Read AGENTS.md, setup.md, PROFILE_QUESTIONS.md, profile.example.json, and
resume.example.json completely. Help me configure applyd locally. Keep test mode
enabled. Never submit an application, add hosted infrastructure, expose a
secret, or invent profile/resume facts. Ask me for consequential missing facts,
write only local ignored configuration files, run the documented verification
commands, and explain every failure in plain language.
```

Do not give an agent API keys in chat. Put secrets directly in your local
`.env`. Before accepting generated profile or resume files, review the diff or
open the files yourself.

## Troubleshooting

### `OPENROUTER_API_KEY not set`

Confirm `.env` exists in the repository root and contains a funded, active key.
Restart the command after editing it.

### Chrome exits immediately or says the profile is in use

Use a separate `APPLYD_BROWSER_PROFILE` directory. Close any applyd-launched
Chrome window left from an interrupted run. Do not use Chrome's default profile.

### `tectonic` or `pdfinfo` is missing

Install Tectonic and Poppler, reactivate the virtual environment, and rerun
`applyd tailor <job_id>`.

### A form stops at review

Run:

```bash
applyd trace <job_id> --errors-only
applyd profile-gaps
```

Add only genuinely known facts to `profile.json`, re-run `applyd init`, and
retry. Manual videos, assessments, unsupported ATSes, and unresolved CAPTCHAs
should remain human work.

### Tailoring appears stuck on an OpenRouter response

Each individual tailoring request has a 180-second whole-call deadline in
addition to ordinary network inactivity timeouts. Override it only when a model
provider is known to need longer:

```dotenv
APPLYD_TAILOR_CALL_MAX_SECONDS=240
```

### Reset local state

`data/applyd.sqlite3` is the database. Back it up before removing or replacing
it. Browser sessions are separate under `data/browser/`; deleting a browser
profile signs that automation profile out of its sites.

## Verify a development checkout

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/python -m applyd.cli --help
```
