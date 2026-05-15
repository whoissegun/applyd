"""Smoke test: end-to-end tailor_for_user against live Supabase + Anthropic.

This run will cost roughly $0.04 in Anthropic spend (one Sonnet 4.6 call with
the master resume cached). That's expected — the only way to know the wire-up
works is to actually pay for one round trip.

Usage:
    .venv/bin/python scripts/smoke_tailor_saas.py
"""

from __future__ import annotations

import os
import secrets
import sys
import time
import traceback
from pathlib import Path
from typing import List, Optional, Tuple

from supabase import create_client

# Make the src layout importable when running as a script.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from applyd.config import load_env  # noqa: E402

load_env()

from applyd.db import (  # noqa: E402
    ApplicationsRepo,
    TailoredResumesRepo,
    UserResumesRepo,
    get_client,
)
from applyd.tailor.saas import STORAGE_BUCKET, tailor_for_user  # noqa: E402
from applyd.worker import tailor_runner  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
results: List[Tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {name}{(' — ' + detail) if detail else ''}")
    results.append((name, ok, detail))


def _admin_client():
    return create_client(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"]
    )


def _pick_job_with_description(admin) -> Optional[dict]:
    res = (
        admin.table("jobs")
        .select("id, title, description")
        .not_.is_("description", "null")
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def main() -> int:
    admin = _admin_client()
    sb = get_client()

    user_id: Optional[str] = None
    application_id: Optional[str] = None
    application_id_2: Optional[str] = None
    job_id: Optional[str] = None
    storage_path: Optional[str] = None
    storage_path_2: Optional[str] = None

    try:
        # 1. Create throwaway auth user.
        email = f"smoke+tailor+{secrets.token_hex(6)}@applyd.test"
        password = secrets.token_urlsafe(16)
        created = admin.auth.admin.create_user(
            {"email": email, "password": password, "email_confirm": True}
        )
        user = getattr(created, "user", None)
        user_id = getattr(user, "id", None) if user else None
        check("1. create throwaway auth user", bool(user_id), f"user_id={user_id}")
        if not user_id:
            return 1

        # 2. Seed user_resumes from repo's resume_base.tex.
        base_path = REPO_ROOT / "resume_base.tex"
        if not base_path.exists():
            check("2. seed user_resumes", False, f"missing {base_path}")
            return 1
        base_tex = base_path.read_text(encoding="utf-8")
        resumes = UserResumesRepo(sb)
        resumes.set_latex(user_id, base_tex)
        got = resumes.get(user_id)
        check(
            "2. seed user_resumes from resume_base.tex",
            got is not None and got.get("latex_source") == base_tex,
            f"len={len(base_tex)}",
        )

        # 3. Pick a job from the catalog with a real description.
        job = _pick_job_with_description(admin)
        if job is None:
            check("3. find a job with description", False, "no rows")
            return 1
        job_id = job["id"]
        check(
            "3. find a job with description",
            True,
            f"job_id={job_id} title={job.get('title')!r}",
        )

        # 4. Create application via upsert_pending.
        apps = ApplicationsRepo(sb)
        app_row = apps.upsert_pending(user_id, job_id)
        application_id = app_row["id"]
        check(
            "4. upsert_pending -> application",
            app_row.get("status") == "pending",
            f"id={application_id} status={app_row.get('status')}",
        )

        # 5. Run tailor_for_user — the meat of the smoke.
        t0 = time.time()
        result = tailor_for_user(user_id, job_id)
        elapsed = time.time() - t0
        ok = (
            result.get("status") == "tailored"
            and (result.get("cost_cents") or 0) > 0
        )
        check(
            "5. tailor_for_user returns 'tailored' with cost > 0",
            ok,
            f"status={result.get('status')} "
            f"cost_cents={result.get('cost_cents')} "
            f"tokens={result.get('tokens')} "
            f"elapsed={elapsed:.1f}s",
        )
        if not ok:
            print("  full result:", result)
            return 1

        tailored_resume_id = result["tailored_resume_id"]
        storage_path = result["pdf_storage_path"]

        # 6. tailored_resumes row exists.
        tr_repo = TailoredResumesRepo(sb)
        tr_row = tr_repo.get(user_id, job_id)
        check(
            "6. tailored_resumes row present",
            tr_row is not None and tr_row.get("id") == tailored_resume_id,
            f"id={tailored_resume_id}",
        )

        # 7. PDF exists in Storage and is non-trivial.
        try:
            pdf_bytes = sb.storage.from_(STORAGE_BUCKET).download(storage_path)
        except Exception as exc:  # noqa: BLE001
            pdf_bytes = b""
            print(f"  storage download error: {exc}")
        check(
            "7. PDF in Storage at expected path",
            len(pdf_bytes) > 1000 and pdf_bytes[:4] == b"%PDF",
            f"path={storage_path} bytes={len(pdf_bytes)}",
        )

        # 8. usage_events row recorded.
        usage_res = (
            admin.table("usage_events")
            .select("id, event_type, cost_cents, metadata")
            .eq("user_id", user_id)
            .eq("event_type", "tailor")
            .execute()
        )
        match = [
            row
            for row in (usage_res.data or [])
            if (row.get("metadata") or {}).get("application_id") == application_id
        ]
        check(
            "8. usage_events row recorded for tailor",
            len(match) == 1 and match[0].get("cost_cents", 0) > 0,
            f"matches={len(match)}",
        )

        # 9. application status -> 'tailored' with FK populated.
        app_res = (
            admin.table("applications")
            .select("status, tailored_resume_id")
            .eq("id", application_id)
            .execute()
        )
        cur = app_res.data[0] if app_res.data else {}
        check(
            "9. application transitioned to 'tailored'",
            cur.get("status") == "tailored"
            and cur.get("tailored_resume_id") == tailored_resume_id,
            f"status={cur.get('status')} fk={cur.get('tailored_resume_id')}",
        )

        # 10. Bonus: drive the worker tick_once on a fresh pending app.
        #
        # Pick a *different* job so the existing tailored row doesn't shadow
        # this run. If only one job has a description we'll skip the bonus.
        other = (
            admin.table("jobs")
            .select("id, title")
            .not_.is_("description", "null")
            .neq("id", job_id)
            .limit(1)
            .execute()
        )
        if other.data:
            job_id_2 = other.data[0]["id"]
            app_row_2 = apps.upsert_pending(user_id, job_id_2)
            application_id_2 = app_row_2["id"]
            tick_result = tailor_runner.tick_once()
            ok2 = (
                tick_result is not None
                and tick_result.get("status") == "tailored"
                and tick_result.get("application_id") == application_id_2
            )
            check(
                "10. tailor_runner.tick_once() processed pending app",
                ok2,
                f"tick={tick_result.get('status') if tick_result else None}",
            )
            if ok2:
                storage_path_2 = tick_result.get("pdf_storage_path")
        else:
            check("10. tailor_runner.tick_once() processed pending app", True,
                  "skipped — only one job with description available")

        return 0 if all(ok for _, ok, _ in results) else 1

    except Exception:
        traceback.print_exc()
        check("smoke run", False, "exception")
        return 1
    finally:
        # ---------- cleanup ----------
        try:
            for path in (storage_path, storage_path_2):
                if path:
                    try:
                        sb.storage.from_(STORAGE_BUCKET).remove([path])
                    except Exception as exc:  # noqa: BLE001
                        print(f"[cleanup] storage remove failed for {path}: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[cleanup] storage cleanup outer error: {exc}")

        # Tailored + application rows cascade off the auth user delete; do
        # it explicitly anyway for visibility.
        try:
            if user_id:
                admin.table("tailored_resumes").delete().eq(
                    "user_id", user_id
                ).execute()
                admin.table("applications").delete().eq(
                    "user_id", user_id
                ).execute()
        except Exception as exc:  # noqa: BLE001
            print(f"[cleanup] row cleanup failed: {exc}")

        try:
            if user_id:
                admin.auth.admin.delete_user(user_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[cleanup] delete_user failed: {exc}")


if __name__ == "__main__":
    sys.exit(main())
