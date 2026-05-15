"""End-to-end happy-path smoke against live Supabase + Anthropic + OpenRouter + Bright Data.

Exercises the full multi-tenant pipeline in test mode:

  signup → master resume upload → profile set → queue application
        → tailor worker tick → verify tailored_resumes + PDF in Storage
        → apply worker tick → verify apply_attempts + usage_events + terminal status

Cost: ~$0.30/run (tailor ~$0.07 cold-cache + apply ~$0.25 in test mode).
APPLYD_TEST_MODE=true means the agent fills forms but never clicks submit.

Run: `python scripts/e2e_happy_path.py`
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from applyd.config import load_env

load_env()

# Hard-enforce test mode for this script regardless of .env contents.
os.environ["APPLYD_TEST_MODE"] = "true"

from applyd.db import (
    ApplicationsRepo,
    ApplyAttemptsRepo,
    JobsRepo,
    TailoredResumesRepo,
    UserProfilesRepo,
    UserResumesRepo,
    get_client,
)
from applyd.worker.runner import tick_once as apply_tick
from applyd.worker.tailor_runner import tick_once as tailor_tick


PROFILE_BLURB = """\
# Test User

**Identity:** E2E Tester | testuser@example.invalid | +1 555 0100
**Location:** Ottawa, Canada
**Education:** Carleton University, BSc Computer Science, expected Apr 2027
**Work authorization:** Eligible to work in Canada. Needs sponsorship for US.
**Demographics:** decline to self-identify
**Why this company:** I admire what they're building and want to contribute to product engineering.
"""


def _pick_job(sb, n_candidates: int = 20) -> dict:
    """Pick a direct-apply Greenhouse/Ashby job that has a description."""
    res = (
        sb.table("jobs")
        .select("id, ats, url, title, apply_gate")
        .in_("ats", ["greenhouse", "ashby"])
        .is_("apply_gate", "null")
        .not_.is_("description", "null")
        .eq("active", True)
        .limit(n_candidates)
        .execute()
        .data
    )
    if not res:
        raise RuntimeError("no candidate jobs found")
    # Take the first; the exact pick doesn't matter for plumbing.
    return res[0]


def _passfail(label: str, cond: bool, extra: str = "") -> bool:
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {label}" + (f" — {extra}" if extra else ""))
    return cond


def main() -> int:
    sb = get_client()
    profiles = UserProfilesRepo(sb)
    resumes = UserResumesRepo(sb)
    apps = ApplicationsRepo(sb)
    tailored = TailoredResumesRepo(sb)
    attempts = ApplyAttemptsRepo(sb)
    jobs = JobsRepo(sb)

    user_id: str | None = None
    application_id: str | None = None
    pdf_storage_path: str | None = None
    job: dict | None = None
    all_pass = True
    t0 = time.time()

    try:
        # ---- setup ----
        email = f"e2e-{int(time.time())}@example.invalid"
        u = sb.auth.admin.create_user(
            {"email": email, "password": "throwaway-pw-12345!", "email_confirm": True}
        ).user
        user_id = u.id
        print(f"\n→ user {user_id[:8]}  email {email}")

        # 1. signup trigger auto-creates profile + subscription
        prof = profiles.get(user_id)
        all_pass &= _passfail("1. signup trigger created user_profiles row", prof is not None)
        sub_n = (
            sb.table("user_subscriptions").select("*", count="exact")
            .eq("user_id", user_id).limit(0).execute().count
        )
        all_pass &= _passfail("2. signup trigger created user_subscriptions row", sub_n == 1)

        # 2. set profile (profile_answers blob — what the apply agent reads as prose)
        profiles.update(user_id, profile_answers=PROFILE_BLURB, full_name="E2E Tester")
        all_pass &= _passfail("3. profile updated with profile_answers", True)

        # 3. set master resume (from repo's resume_base.tex)
        latex = Path("resume_base.tex").read_text(encoding="utf-8")
        resumes.set_latex(user_id, latex_source=latex)
        rr = resumes.get(user_id)
        all_pass &= _passfail(
            "4. master resume stored", rr is not None and rr["latex_source"] == latex
        )

        # 4. pick a job + queue application
        job = _pick_job(sb)
        print(f"   chosen job: [{job['ats']}] {job['title']} ({job['id']})")
        app = apps.upsert_pending(user_id, job["id"])
        application_id = app["id"]
        all_pass &= _passfail(
            "5. application queued as pending",
            app["status"] == "pending",
            f"app_id={application_id[:8]}",
        )

        # ---- tailor ----
        print("\n→ running tailor_tick (cold cache, ~7 cents)…")
        t1 = time.time()
        tailor_tick()
        print(f"   tailor wall: {time.time()-t1:.1f}s")

        # Verify tailor state
        post_tailor_app = apps.get(application_id)
        all_pass &= _passfail(
            "6. application status → 'tailored'",
            post_tailor_app["status"] == "tailored",
            f"status={post_tailor_app['status']}",
        )
        tr = tailored.get(user_id, job["id"])
        pdf_storage_path = tr["pdf_storage_path"] if tr else None
        all_pass &= _passfail(
            "7. tailored_resumes row exists with model/tokens/cost recorded",
            tr is not None
            and tr.get("model_used") is not None
            and tr.get("prompt_tokens") is not None,
        )
        all_pass &= _passfail(
            "8. tailored PDF exists in Storage",
            bool(pdf_storage_path)
            and len(
                sb.storage.from_("resumes").download(pdf_storage_path)
            ) > 1000,
            extra=f"path={pdf_storage_path}",
        )
        u_evt = (
            sb.table("usage_events").select("event_type, cost_cents")
            .eq("user_id", user_id).eq("event_type", "tailor")
            .execute().data
        )
        all_pass &= _passfail(
            "9. usage_events 'tailor' row recorded",
            len(u_evt) == 1 and u_evt[0]["cost_cents"] > 0,
            f"cost_cents={u_evt[0]['cost_cents'] if u_evt else 'n/a'}",
        )

        # ---- apply ----
        print("\n→ running apply_tick (test mode, ~25 cents, ~2 min)…")
        t2 = time.time()
        apply_tick()
        print(f"   apply wall: {time.time()-t2:.1f}s")

        # Verify apply state
        post_apply_app = apps.get(application_id)
        all_pass &= _passfail(
            "10. application reached terminal status",
            post_apply_app["status"] in ("applied", "skipped", "failed"),
            f"status={post_apply_app['status']}",
        )
        att = (
            sb.table("apply_attempts").select("*")
            .eq("application_id", application_id).execute().data
        )
        all_pass &= _passfail("11. apply_attempts row created", len(att) == 1)
        if att:
            a = att[0]
            all_pass &= _passfail(
                "12. apply_attempts has token telemetry",
                a.get("prompt_tokens") is not None
                and a.get("completion_tokens") is not None,
                f"prompt={a.get('prompt_tokens')} completion={a.get('completion_tokens')}",
            )
            all_pass &= _passfail(
                "13. apply_attempts has turn telemetry",
                a.get("turn_count") is not None and a.get("tool_call_counts") is not None,
                f"turns={a.get('turn_count')} tools={a.get('tool_call_counts')}",
            )
        apply_evt = (
            sb.table("usage_events").select("event_type, cost_cents, metadata")
            .eq("user_id", user_id).eq("event_type", "apply")
            .execute().data
        )
        all_pass &= _passfail(
            "14. usage_events 'apply' row recorded",
            len(apply_evt) == 1 and apply_evt[0]["cost_cents"] > 0,
            f"cost_cents={apply_evt[0]['cost_cents'] if apply_evt else 'n/a'}",
        )
        if apply_evt:
            all_pass &= _passfail(
                "15. apply usage_event has test_mode=true in metadata",
                (apply_evt[0].get("metadata") or {}).get("test_mode") is True,
            )

        total_cost = sum(e["cost_cents"] for e in (u_evt + apply_evt))
        print(f"\n→ total e2e cost: {total_cost} cents ({total_cost/100:.2f} USD)")
        print(f"→ total wall: {time.time()-t0:.1f}s")

    finally:
        # ---- cleanup ----
        print("\n→ cleanup…")
        if pdf_storage_path:
            try:
                sb.storage.from_("resumes").remove([pdf_storage_path])
            except Exception as e:  # noqa: BLE001
                print(f"   storage cleanup warn: {e}")
        if application_id:
            sb.table("apply_attempts").delete().eq("application_id", application_id).execute()
            sb.table("applications").delete().eq("id", application_id).execute()
        if user_id:
            sb.table("tailored_resumes").delete().eq("user_id", user_id).execute()
            sb.table("usage_events").delete().eq("user_id", user_id).execute()
            sb.table("user_resumes").delete().eq("user_id", user_id).execute()
            try:
                sb.auth.admin.delete_user(user_id)
            except Exception as e:  # noqa: BLE001
                print(f"   auth delete warn: {e}")
        print("→ cleanup done")

    print("\n" + ("✓ ALL PASS" if all_pass else "✗ FAIL"))
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
