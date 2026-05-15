"""Smoke test: boots the FastAPI app + exercises the worker shell.

Runs against the live Supabase project. Creates a throwaway auth user, runs
through the endpoints, runs a single worker tick, then cleans up everything it
touched.

Usage:
    .venv/bin/python scripts/smoke_api_and_worker.py
"""

from __future__ import annotations

import os
import secrets
import sys
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import httpx
import uvicorn
from supabase import create_client

# Make the src layout importable when running as a script.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from applyd.config import load_env  # noqa: E402

load_env()

from applyd.api.app import app  # noqa: E402
from applyd.worker import runner as worker_runner  # noqa: E402


PORT = 18742  # arbitrary, unlikely to collide
BASE = f"http://127.0.0.1:{PORT}"
JOB_ID = f"smoke-job-{secrets.token_hex(4)}"

results: List[Tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {name}{(' — ' + detail) if detail else ''}")
    results.append((name, ok, detail))


# ---------- uvicorn in a thread ----------


class _ServerThread(threading.Thread):
    def __init__(self) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
        self.server = uvicorn.Server(config)
        super().__init__(daemon=True)

    def run(self) -> None:
        self.server.run()


def _wait_for_boot(timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{BASE}/healthz", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


# ---------- supabase admin helpers ----------


def _admin_client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])


def _anon_client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_PUBLISHABLE_KEY"])


def main() -> int:
    server = _ServerThread()
    server.start()

    if not _wait_for_boot():
        check("server boots", False, "uvicorn did not respond to /healthz")
        return 1

    admin = _admin_client()
    anon = _anon_client()

    user_id: Optional[str] = None
    jwt: Optional[str] = None
    job_inserted = False

    try:
        # 1. /healthz no auth
        r = httpx.get(f"{BASE}/healthz")
        check("1. GET /healthz", r.status_code == 200 and r.json().get("ok") is True,
              f"status={r.status_code}")

        # 2. /profile without auth -> 401
        r = httpx.get(f"{BASE}/profile")
        check("2. GET /profile without auth -> 401", r.status_code == 401,
              f"status={r.status_code}")

        # 3. create throwaway auth user
        email = f"smoke+{secrets.token_hex(6)}@applyd.test"
        password = secrets.token_urlsafe(16)
        created = admin.auth.admin.create_user(
            {"email": email, "password": password, "email_confirm": True}
        )
        user = getattr(created, "user", None)
        user_id = getattr(user, "id", None) if user else None
        check("3. create throwaway auth user", bool(user_id), f"user_id={user_id}")
        if not user_id:
            return 1

        # 4. sign in to get a real JWT
        signed_in = anon.auth.sign_in_with_password({"email": email, "password": password})
        session = getattr(signed_in, "session", None)
        jwt = getattr(session, "access_token", None) if session else None
        check("4. sign in -> JWT", bool(jwt), f"got_jwt={bool(jwt)}")
        if not jwt:
            return 1

        auth_h = {"Authorization": f"Bearer {jwt}"}

        # 5. /profile with JWT -> 200, trigger created the row
        r = httpx.get(f"{BASE}/profile", headers=auth_h)
        body: Dict[str, Any] = r.json() if r.status_code == 200 else {}
        check(
            "5. GET /profile with JWT",
            r.status_code == 200 and body.get("id") == user_id,
            f"status={r.status_code} id={body.get('id')}",
        )

        # 6. PUT /profile
        r = httpx.put(
            f"{BASE}/profile",
            headers=auth_h,
            json={"profile_answers": "test"},
        )
        check(
            "6. PUT /profile",
            r.status_code == 200 and r.json().get("profile_answers") == "test",
            f"status={r.status_code}",
        )

        # 7. PUT /resume
        latex = "\\documentclass{article}\\begin{document}smoke\\end{document}"
        r = httpx.put(
            f"{BASE}/resume",
            headers=auth_h,
            json={"latex_source": latex},
        )
        check(
            "7. PUT /resume",
            r.status_code == 200 and r.json().get("latex_source") == latex,
            f"status={r.status_code}",
        )

        # 8. GET /resume
        r = httpx.get(f"{BASE}/resume", headers=auth_h)
        check(
            "8. GET /resume",
            r.status_code == 200 and r.json().get("latex_source") == latex,
            f"status={r.status_code}",
        )

        # 9. POST /tailor/queue — first we need a real job row to satisfy the FK
        admin.table("jobs").insert(
            {
                "id": JOB_ID,
                "url": f"https://example.test/jobs/{JOB_ID}",
                "title": "Smoke Test Job",
            }
        ).execute()
        job_inserted = True

        r = httpx.post(
            f"{BASE}/tailor/queue",
            headers=auth_h,
            json={"job_id": JOB_ID},
        )
        app_row = (r.json() or {}).get("application") if r.status_code == 200 else {}
        application_id = (app_row or {}).get("id")
        check(
            "9. POST /tailor/queue -> pending application",
            r.status_code == 200
            and (app_row or {}).get("status") == "pending"
            and (app_row or {}).get("job_id") == JOB_ID,
            f"status={r.status_code} app_status={(app_row or {}).get('status')}",
        )

        # 10. Seed application -> 'tailored' so the worker can claim it.
        if application_id:
            admin.table("applications").update({"status": "tailored"}).eq(
                "id", application_id
            ).execute()
            check("10. seed application as 'tailored'", True, f"id={application_id}")
        else:
            check("10. seed application as 'tailored'", False, "no application_id")
            return 1

        # 11. worker.tick_once() — sleep shortened so the smoke is snappy
        released = worker_runner.tick_once(simulate_work_seconds=0.1)
        check(
            "11. worker.tick_once() processed a row",
            released is not None and released.get("id") == application_id,
            f"released_id={(released or {}).get('id')}",
        )

        # 12. row is now 'applied'
        r = admin.table("applications").select("status").eq("id", application_id).execute()
        cur_status = r.data[0]["status"] if r.data else None
        check(
            "12. application status -> 'applied'",
            cur_status == "applied",
            f"status={cur_status}",
        )

        # 13. usage_events row recorded
        r = (
            admin.table("usage_events")
            .select("id, event_type, cost_cents, metadata")
            .eq("user_id", user_id)
            .eq("event_type", "apply")
            .execute()
        )
        match = [
            row
            for row in (r.data or [])
            if (row.get("metadata") or {}).get("application_id") == application_id
        ]
        check(
            "13. usage_events row recorded for apply",
            len(match) >= 1,
            f"matches={len(match)}",
        )

        return 0 if all(ok for _, ok, _ in results) else 1

    except Exception:
        traceback.print_exc()
        check("smoke run", False, "exception")
        return 1
    finally:
        # ---------- cleanup ----------
        try:
            if user_id:
                # Cascades take care of applications, apply_attempts, tailored_resumes,
                # user_resumes, user_profiles, usage_events.
                admin.auth.admin.delete_user(user_id)
        except Exception as exc:
            print(f"[cleanup] delete_user failed: {exc}")
        try:
            if job_inserted:
                admin.table("jobs").delete().eq("id", JOB_ID).execute()
        except Exception as exc:
            print(f"[cleanup] delete job failed: {exc}")

        server.server.should_exit = True
        time.sleep(0.2)


if __name__ == "__main__":
    sys.exit(main())
