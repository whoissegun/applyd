# applyd — TODO

Snapshot taken 2026-05-15. Pick up here.

---

## Direction we just committed to

**Drop the LaTeX-only input requirement. Make Resume JSON the canonical form.**

The user uploads anything — LaTeX, plain text, or PDF — we extract to a
canonical `Resume` (Pydantic), tailor on that JSON per job, then render
through *our* Jake's-template LaTeX renderer and compile to PDF with
tectonic. Jake's stays as our rendering layer (we own it). The user never
needs to know LaTeX.

Why: every "soften the copy" pass on the resume page was fighting the
underlying product choice. Asking non-technical users to paste LaTeX is
the single biggest funnel-killer in onboarding.

LaTeX paste stays as a *recommended* input mode (lossless extraction via
regex, no LLM cost). Everyone else gets vision/text extraction.

---

## What landed this session

### Backend (`src/applyd/resume/`)
- **`schema.py`** — canonical `Resume` Pydantic model (contact, education, experience, projects, skills). Bullets are plain strings, no LaTeX, no markdown — emphasis added at render time.
- **`from_latex.py`** — Jake's-template LaTeX → JSON. Balanced-brace parser, no LLM. Power-user lossless path.
- **`render_latex.py`** — JSON → Jake's-template LaTeX. Deterministic. Optional `auto_bold` heuristic for percentages and unit-bearing numbers.
- Round-trip verified on real `resume_base.tex` → JSON → LaTeX → PDF. Compiles cleanly with tectonic. Output PDF in `out/roundtrip/`.

### Frontend (`frontend/`)
- ElevenLabs design system applied across all pages (landing, auth, onboarding, dashboard).
- `src/lib/devflags.ts` — single source of truth for dev flags + mock fixtures. `DEV_FLAGS.bypassAuth = true` skips Supabase entirely. Production-safe (NODE_ENV-guarded).
- Auth pages (`/login`, `/signup`) use eggshell theme with Cormorant 300 serif headlines. Signup is email+password only (full_name moves to onboarding). Password has eye-toggle.
- Onboarding flow reordered: **resume → basics → work-auth**. Basics/work-auth are framed as "we pulled these from your resume — edit anything that's wrong" once LLM extraction is wired.
- Landing has the "Always on · applies 24/7" live indicator + italic "Around the clock. Even while you sleep." emphasis line.

### Infra
- `Dockerfile` + `DEPLOY.md` for Railway 6-service deployment (api, 3 workers, 2 crons).

---

## Open work

### 1. Frontend: multi-input resume UI ⭐ NEXT
- Onboarding step 1 + `/dashboard/resume` settings page get a 3-way input:
  - **Paste LaTeX** (recommended, labelled best fidelity)
  - **Paste plain text**
  - **Upload PDF**
- Tip text explaining why LaTeX is recommended (regex extract = lossless, others go through Claude).
- All three flow to the same `extract → Resume JSON` shape downstream.

### 2. DB migration — `resume_json` column
- Add `resume_json jsonb` to `public.user_resumes` alongside `latex_source`.
- Add `tailored_resume_json jsonb` to `public.tailored_resumes`.
- Backfill from existing `latex_source` rows by calling `extract_from_latex`.
- File: new migration in `supabase/migrations/`.

### 3. Tailor refactor (`src/applyd/tailor/`)
- Tailor prompt: accept `Resume` JSON + JD, emit tailored `Resume` JSON.
- Slim `validate.py` to JSON-schema + no-invention checks. Drop brace-balance / `\section{}` regex.
- Wire `tailor_for_user` (in `tailor/saas.py`):
  1. Load `user_resumes.resume_json` (or extract from `latex_source` on the fly).
  2. Call new JSON tailor.
  3. Render with `resume.render_latex`.
  4. Compile with tectonic.
  5. Persist tailored JSON + PDF as before.

### 4. PDF + text extractors (`src/applyd/resume/`)
- **`from_pdf.py`** — Claude vision call. Renders page as image, prompts for structured JSON matching the schema.
- **`from_text.py`** — Claude text call. Same JSON output.
- Both should validate output against the Pydantic schema and retry on parse failure.

### 5. Backend extraction endpoint (FastAPI)
- New route in `src/applyd/api/` — `POST /resume/extract` taking `{ kind: 'latex'|'text'|'pdf', payload: string|base64 }` → `Resume` JSON.
- Frontend calls this after the user uploads/pastes in the multi-input UI.
- Also: extract once on resume save to also populate `user_profiles` (full_name, phone, linkedin_url, github_url, portfolio_url, work_auth_summary, sponsorship_needed_countries) so the onboarding basics + work-auth steps land prefilled.

### 6. Smaller follow-ups
- Pricing section copy currently still says "50 applications free. Then a flat monthly price. Cancel anytime." — soften or remove until real pricing.
- Resume page tip text still hints at LaTeX even in the simplified copy ("paste your resume here") — re-align once multi-input UI exists.
- Settings.local.json still has `WebFetch(domain:www.browserbase.com)` (harness blocked self-edit earlier). Drop manually.

---

## Quick start for next session

```bash
# Backend Python (already verified working)
cd /Users/divinejojolola/Desktop/applyd
.venv/bin/python -c "from applyd.resume import extract_from_latex, render_latex; \
  print('OK')"

# Round-trip test
.venv/bin/python -c "
from pathlib import Path
from applyd.resume import extract_from_latex, render_latex
r = extract_from_latex(Path('resume_base.tex').read_text())
Path('out/roundtrip/resume_rendered.tex').write_text(render_latex(r))
"
tectonic --outdir out/roundtrip out/roundtrip/resume_rendered.tex

# Frontend (dev bypass is on — no Supabase needed)
cd frontend
npm run dev
# → http://localhost:3000
```

Bypass flag lives at `frontend/src/lib/devflags.ts` — flip `bypassAuth: false` to talk to a real Supabase project (which means setting the two `NEXT_PUBLIC_SUPABASE_*` vars in `frontend/.env.local`).
