# applyd contributor guide

## Direction

applyd is local-first and CLI-first, not a multi-tenant SaaS. The state store is
SQLite and the default browser is persistent local Chrome. Keep model boundaries
swappable, but use Kimi K2.6 through OpenRouter until a local model passes the
same evals.

Pipeline: discover via aggregators/ATS/search; retrieve via ATS API, HTTP, then
local Playwright; extract grounded facts with Kimi; evaluate with deterministic
profile policy; tailor through structured Kimi JSON and deterministic LaTeX;
apply through Kimi's bounded browser tool loop.

## Non-negotiable behavior

- Do not add Supabase, hosted workers, Spider, or Bright Data to the default
  path. Bright Data may remain an explicit optional apply provider.
- ATS adapters own transport normalization; an LLM does not normalize raw ATS
  response formats.
- Consequential extracted facts require verbatim evidence. Unsupported blocking
  conclusions reset to neutral values.
- Unknown eligibility facts default to eligible unless the profile asks for
  review. Maximize funnel volume.
- Identity, legal status, dates, credentials, employers, technologies, metrics,
  and historical events must be grounded.
- Kimi may compose motivation, opinions, tone, and reasonable purpose language.
- Tailoring uses source bullet IDs. Python owns LaTeX; the model never writes it.
- Test mode is the apply default. A real run is `--test-mode false`.
- Navigation and resume upload are runner-bound, not model-selected.
- Persist `applied` only after submit confirmation that the model observed in a
  later turn.

## Important files

- `src/applyd/local_store.py` — SQLite repositories
- `src/applyd/discovery/` — discovery and ATS adapters
- `src/applyd/enrichment/` — retrieval and semantic extraction
- `src/applyd/eligibility.py` — deterministic user policy
- `src/applyd/tailor/structured.py` — edit plan and LaTeX renderer
- `src/applyd/apply/` — browser loop
- `profile.example.json`, `resume.example.json`, `resume_template.tex`

## Verification

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/python -m applyd.cli --help
```

When resume rendering changes, compile and visually inspect a real PDF. Preserve
user-owned files and unrelated changes. Avoid speculative abstractions.
