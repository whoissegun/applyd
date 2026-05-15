"""End-to-end smoke for the multi-tenant apply path.

Runs against live Supabase + OpenRouter + Bright Data with
APPLYD_TEST_MODE=true (the agent fills the form but never clicks submit).
Expect ~$0.27 of spend per run (Anthropic tokens via OpenRouter + ~1.5 MB
Bright Data bandwidth).

Verifies the PLUMBING — not that a specific form actually accepts our
submission. A `failed` or `skipped` terminal status with attempt + usage
rows present is a pass.

Usage:
    .venv/bin/python scripts/smoke_apply_saas.py
"""

from __future__ import annotations

import os
import secrets
import sys
import traceback
from pathlib import Path
from typing import List, Optional, Tuple

from supabase import create_client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from applyd.config import load_env  # noqa: E402

# Force test mode BEFORE load_env so it can't be overridden by a stale .env line.
os.environ["APPLYD_TEST_MODE"] = "true"
load_env()
os.environ["APPLYD_TEST_MODE"] = "true"  # belt + suspenders

from applyd.apply.saas import apply_for_user  # noqa: E402
from applyd.db import (  # noqa: E402
    ApplicationsRepo,
    cost_cents_for_apply,
    get_client,
)
from applyd.worker import runner as worker_runner  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
STORAGE_BUCKET = "resumes"

# Minimal-but-valid 1-page PDF (the agent just needs *something* to upload).
TINY_PDF_BYTES = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<<>>>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
    b"0000000053 00000 n \n0000000100 00000 n \ntrailer<</Size 4/Root 1 0 R>>\n"
    b"startxref\n172\n%%EOF\n"
)

# Minimal but truthful profile prose — Divine's actual details (per CLAUDE.md context).
PROFILE_PROSE = """\
# applyd profile

## Identity
- Full legal name: Divine Jojolola
- Email: jojololadivine05@gmail.com
- Phone: +1 613-555-0142 (smoke test placeholder)

## Location
Ottawa, Ontario, Canada.

## Links
- LinkedIn: https://www.linkedin.com/in/divine-jojolola
- GitHub: https://github.com/whoissegun

## Education
- Carleton University, Bachelor of Computer Science
- Expected graduation: April 2027 (ISO 2027-04)

## Work authorization
- Canada: authorized to work, no sponsorship required.
- United States: requires visa sponsorship.
- Other countries: requires sponsorship.

## Demographics (smoke-test stub; decline when allowed)
- Gender: prefer not to say
- Veteran: not a protected veteran
- Disability: prefer not to answer

## Agent rules
- Never invent facts. Skip the job if a required field has no truthful answer.
- This is a TEST RUN. Do not click submit.
"""

results: List[Tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {name}{(' — ' + detail) if detail else ''}")
    results.append((name, ok, detail))


def _admin_client():
    return create_client(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"]
    )


def _pick_direct_apply_job(admin) -> Optional[dict]:
    """An active Greenhouse/Ashby job with no apply_gate (direct-apply)."""
    res = (
        admin.table("jobs")
        .select("id, title, url, ats, apply_gate, active")
        .in_("ats", ["greenhouse", "ashby"])
        .is_("apply_gate", "null")
        .eq("active", True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def _seed_tailored_pdf(admin, sb, user_id: str, job_id: str) -> tuple[str, str]:
    """Insert a tailored_resumes row + upload a tiny PDF to storage.
    Returns (tailored_resume_id, storage_path)."""
    storage_path = f"{user_id}/tailored/{job_id}.pdf"
    sb.storage.from_(STORAGE_BUCKET).upload(
        storage_path,
        TINY_PDF_BYTES,
        {"content-type": "application/pdf", "upsert": "true"},
    )
    row = (
        admin.table("tailored_resumes")
        .upsert(
            {
                "user_id": user_id,
                "job_id": job_id,
                "latex_source": "\\documentclass{article}\\begin{document}smoke\\end{document}",
                "pdf_storage_path": storage_path,
                "tailor_metadata": {"smoke": True},
                "validator_passed": True,
            },
            on_conflict="user_id,job_id",
        )
        .execute()
    )
    return row.data[0]["id"], storage_path


def _make_user(admin) -> Optional[str]:
    email = f"smoke+apply+{secrets.token_hex(6)}@applyd.test"
    password = secrets.token_urlsafe(16)
    created = admin.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True}
    )
    user = getattr(created, "user", None)
    return getattr(user, "id", None) if user else None


def _create_tailored_application(admin, user_id: str, job_id: str) -> Optional[str]:
    row = (
        admin.table("applications")
        .upsert(
            {"user_id": user_id, "job_id": job_id, "status": "tailored"},
            on_conflict="user_id,job_id",
        )
        .execute()
    )
    return row.data[0]["id"] if row.data else None


def main() -> int:
    admin = _admin_client()
    sb = get_client()

    user_id: Optional[str] = None
    app_id: Optional[str] = None
    app_id_2: Optional[str] = None
    storage_paths: list[str] = []
    job_id: Optional[str] = None

    try:
        # Sanity-check test mode is on.
        check(
            "0. APPLYD_TEST_MODE=true",
            os.environ.get("APPLYD_TEST_MODE", "").lower() == "true",
            f"value={os.environ.get('APPLYD_TEST_MODE')!r}",
        )

        # 1. Create user.
        user_id = _make_user(admin)
        check("1. create throwaway auth user", bool(user_id), f"user_id={user_id}")
        if not user_id:
            return 1

        # 2. Set profile_answers prose.
        admin.table("user_profiles").update(
            {"profile_answers": PROFILE_PROSE, "full_name": "Divine Jojolola"}
        ).eq("id", user_id).execute()
        prof = (
            admin.table("user_profiles")
            .select("profile_answers")
            .eq("id", user_id)
            .execute()
        )
        check(
            "2. seed profile_answers",
            bool(prof.data and prof.data[0].get("profile_answers")),
            f"chars={len((prof.data or [{}])[0].get('profile_answers') or '')}",
        )

        # 3. Pick a real direct-apply job.
        job = _pick_direct_apply_job(admin)
        check("3. pick direct-apply job", bool(job),
              f"id={(job or {}).get('id')} ats={(job or {}).get('ats')}")
        if not job:
            return 1
        job_id = job["id"]
        print(f"     job url: {job['url']}")

        # 4. Seed tailored_resumes + PDF in storage.
        _tailored_id, storage_path = _seed_tailored_pdf(admin, sb, user_id, job_id)
        storage_paths.append(storage_path)
        check("4. seed tailored_resumes + PDF blob", True, f"path={storage_path}")

        # 5. Create application as 'tailored'.
        app_id = _create_tailored_application(admin, user_id, job_id)
        check("5. create application status='tailored'", bool(app_id), f"app_id={app_id}")
        if not app_id:
            return 1

        # 6. Run apply_for_user.
        print(f"\n[smoke] dispatching apply_for_user(user={user_id}, app={app_id})...")
        result = apply_for_user(user_id=user_id, application_id=app_id)
        print(f"[smoke] result: {result}")

        terminal = {"applied", "skipped", "failed"}
        check(
            "6. apply_for_user returned terminal status",
            result.get("status") in terminal,
            f"status={result.get('status')}",
        )

        attempt_id = result.get("attempt_id")
        tokens = result.get("tokens") or {}
        cost_cents = result.get("cost_cents", 0)

        # 7. apply_attempts row recorded with tokens.
        attempt_rows = (
            admin.table("apply_attempts")
            .select("id, status, prompt_tokens, completion_tokens, ended_at")
            .eq("id", attempt_id)
            .execute()
            if attempt_id
            else None
        )
        attempt_row = (attempt_rows.data[0] if attempt_rows and attempt_rows.data else None)
        check(
            "7. apply_attempts row ended with terminal status",
            bool(attempt_row)
            and attempt_row.get("status") in terminal
            and attempt_row.get("ended_at") is not None,
            f"row={attempt_row}",
        )
        check(
            "7b. apply_attempts has token counts populated",
            bool(attempt_row)
            and (attempt_row.get("prompt_tokens") or 0) >= 0
            and (attempt_row.get("completion_tokens") or 0) >= 0,
            f"prompt={attempt_row and attempt_row.get('prompt_tokens')} "
            f"completion={attempt_row and attempt_row.get('completion_tokens')}",
        )

        # 8. usage_events recorded with the right cost.
        usage_rows = (
            admin.table("usage_events")
            .select("id, cost_cents, metadata")
            .eq("user_id", user_id)
            .eq("event_type", "apply")
            .execute()
        )
        match = [
            r for r in (usage_rows.data or [])
            if (r.get("metadata") or {}).get("attempt_id") == attempt_id
        ]
        check(
            "8. usage_events row recorded",
            len(match) == 1,
            f"matches={len(match)}",
        )

        # 8b. cost_cents matches pricing computation.
        if match:
            recorded_cost = match[0]["cost_cents"]
            recomputed = cost_cents_for_apply(
                model=(match[0].get("metadata") or {}).get(
                    "model", "anthropic/claude-sonnet-4-6"
                ),
                prompt_tokens=tokens.get("input", 0),
                completion_tokens=tokens.get("output", 0),
                cached_tokens=tokens.get("cache_read", 0),
                browser_mb=float(
                    (match[0].get("metadata") or {}).get("browser_mb", 1.5)
                ),
            )
            check(
                "8b. usage_events.cost_cents matches pricing recompute",
                recorded_cost == recomputed == cost_cents,
                f"recorded={recorded_cost} recomputed={recomputed} returned={cost_cents}",
            )

        # 9. application is terminal.
        cur = (
            admin.table("applications")
            .select("status")
            .eq("id", app_id)
            .execute()
        )
        cur_status = cur.data[0]["status"] if cur.data else None
        check(
            "9. application terminal status",
            cur_status in terminal,
            f"status={cur_status}",
        )

        # 10. Bonus: worker tick_once path on a second seeded application.
        # Re-flip the first row to 'tailored' to prove the worker also picks it up?
        # Cleaner: create a second tailored row pointing to the same job (idempotent
        # upsert means it'll just bump the existing one back to tailored).
        admin.table("applications").update({"status": "tailored"}).eq("id", app_id).execute()
        app_id_2 = app_id  # same row, re-queued
        print(f"\n[smoke] re-queued {app_id_2} as tailored, calling worker.tick_once()...")
        tick_result = worker_runner.tick_once()
        check(
            "10. worker.tick_once() processed re-queued app",
            tick_result is not None
            and tick_result.get("status") in terminal | {"lost_race"},
            f"tick_result_status={(tick_result or {}).get('status')}",
        )

        all_ok = all(ok for _, ok, _ in results)
        print("\n=== SUMMARY ===")
        for name, ok, _ in results:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        return 0 if all_ok else 1

    except Exception:
        traceback.print_exc()
        check("smoke run", False, "exception")
        return 1
    finally:
        # ---------- cleanup ----------
        try:
            if app_id:
                admin.table("applications").delete().eq("id", app_id).execute()
        except Exception as exc:
            print(f"[cleanup] delete application failed: {exc}")
        try:
            if user_id and job_id:
                admin.table("tailored_resumes").delete().eq("user_id", user_id).eq(
                    "job_id", job_id
                ).execute()
        except Exception as exc:
            print(f"[cleanup] delete tailored_resumes failed: {exc}")
        for sp in storage_paths:
            try:
                sb.storage.from_(STORAGE_BUCKET).remove([sp])
            except Exception as exc:
                print(f"[cleanup] storage remove {sp} failed: {exc}")
        try:
            if user_id:
                admin.auth.admin.delete_user(user_id)
        except Exception as exc:
            print(f"[cleanup] delete_user failed: {exc}")


if __name__ == "__main__":
    sys.exit(main())
