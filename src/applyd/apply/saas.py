"""Multi-tenant apply entrypoint.

`apply_for_user(user_id, application_id)` is the worker-facing function. It:

  1. Claims the application atomically (status -> 'in_progress').
  2. Opens an apply_attempts row.
  3. Loads the per-user profile, tailored resume + PDF, and the job row.
  4. Calls into the existing tool-use loop (`runner.run_apply`).
  5. Records cost + usage + final attempt state, releases the application.

All persistence happens through `applyd.db` repos using the service-role
client (RLS bypass). Every step is best-effort idempotent so a crashed
worker leaves a `failed` row instead of a stuck `in_progress` one.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from ..config import load_env
from ..discovery.routing import preferred_apply_url
from ..llm_errors import TransientInfraError
from ..db import (
    ApplicationsRepo,
    ApplyAttemptsRepo,
    JobsRepo,
    TailoredResumesRepo,
    UsageEventsRepo,
    UserProfilesRepo,
    cost_cents_for_apply,
    get_client,
)
from .runner import DEFAULT_MODEL, run_apply

logger = logging.getLogger("applyd.apply.saas")

CLAIMABLE_FROM: tuple[str, ...] = ("tailored", "failed")

# Force an application terminal after this many attempts, whatever the reason —
# a backstop so no failure class can loop paid retries forever.
MAX_APPLY_ATTEMPTS = 3

# Skip notes that describe the JOB, not this user: feed them back to the
# shared jobs row so ranking/tailor/apply stop routing other tenants at the
# same wall. Captcha stays per-user (often session/IP luck), and
# missing_info / jd_mismatch depend on the candidate.
_JOB_LEVEL_GATES: dict[str, str] = {
    "gated:login_required": "login_required",
    "gated:signup_required": "signup_required",
    "gated:cover_letter_required": "cover_letter_required",
    "skipped:coding_challenge": "coding_challenge",
}


def _propagate_job_gate(jobs: JobsRepo, job_id: str, status: str, note: str | None) -> None:
    """Best-effort: a failure here must never affect the apply result."""
    if status != "skipped" or not note:
        return
    try:
        if note.startswith("gated:dead_link"):
            jobs.mark_inactive(job_id, "dead_link_at_apply")
            logger.info("[apply] marked job %s inactive (dead link)", job_id)
            return
        for prefix, gate in _JOB_LEVEL_GATES.items():
            if note.startswith(prefix):
                jobs.set_apply_gate(job_id, gate)
                logger.info("[apply] set apply_gate=%s on job %s", gate, job_id)
                return
    except Exception:  # noqa: BLE001
        logger.exception("[apply] failed to propagate gate for job %s", job_id)


def _contact_block(profile: dict[str, Any]) -> str:
    """The literal form-fill fields, formatted as a small contact card the
    agent can quote verbatim. Always prepended to the narrative so things like
    'enter your phone' don't require the LLM to fish through prose.
    """
    bits: list[str] = ["# Contact"]
    if name := profile.get("full_name"):
        bits.append(f"- Full legal name: {name}")
    if phone := profile.get("phone"):
        bits.append(f"- Phone: {phone}")
    if linkedin := profile.get("linkedin_url"):
        bits.append(f"- LinkedIn: {linkedin}")
    if github := profile.get("github_url"):
        bits.append(f"- GitHub: {github}")
    if portfolio := profile.get("portfolio_url"):
        bits.append(f"- Portfolio: {portfolio}")
    return "\n".join(bits)


def _build_profile_text(profile: dict[str, Any] | None) -> str:
    if not profile:
        return "(Profile missing — agent should skip the job for any required free-text field.)"
    contact = _contact_block(profile)
    answers = (profile.get("profile_answers") or "").strip()
    if not answers:
        if not any(profile.get(k) for k in ("full_name", "phone", "linkedin_url")):
            return "(Profile is empty — agent should skip the job if any required field is unanswerable.)"
        return contact + "\n\n(No narrative profile_answers on file.)"
    return contact + "\n\n" + answers


def apply_for_user(user_id: str, application_id: str) -> dict[str, Any]:
    """Run one apply for one user. Handles claim/release/attempt/usage internally.

    Returns a dict: {status, application_id, attempt_id, cost_cents, tokens, ...}.
    `status='lost_race'` means another worker claimed the row first.
    """
    load_env()
    sb = get_client()
    apps = ApplicationsRepo(sb)
    attempts = ApplyAttemptsRepo(sb)
    jobs = JobsRepo(sb)
    profiles = UserProfilesRepo(sb)
    tailored = TailoredResumesRepo(sb)
    usage = UsageEventsRepo(sb)

    # 1. application exists?
    application = apps.get(application_id)
    if application is None:
        raise ValueError(f"application {application_id!r} not found")

    # 2. claim atomically.
    claimed = apps.claim(application_id, from_statuses=CLAIMABLE_FROM)
    if claimed is None:
        logger.info("[apply] lost race on %s", application_id)
        return {"status": "lost_race", "application_id": application_id}

    # 3. open the attempt.
    attempt = attempts.insert(
        application_id=application_id,
        user_id=user_id,
        status="pending",
    )
    attempt_id: str = attempt["id"]

    job_id: str = claimed["job_id"]
    tmp_pdf_path: str | None = None
    model = DEFAULT_MODEL
    test_mode = os.environ.get("APPLYD_TEST_MODE", "true").lower() == "true"

    def _finish(
        status: str,
        reason: str | None,
        tokens: dict[str, int] | None = None,
        browser_mb: float = 0.0,
        turn_count: int | None = None,
        tool_call_counts: dict[str, int] | None = None,
        actual_cost_usd: float | None = None,
    ) -> dict[str, Any]:
        tokens = tokens or {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        prompt = tokens.get("input", 0)
        completion = tokens.get("output", 0)
        cached = tokens.get("cache_read", 0)
        written = tokens.get("cache_write", 0)

        try:
            cost_cents = cost_cents_for_apply(
                model=model,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cached_tokens=cached,
                cache_write_tokens=written,
                browser_mb=browser_mb,
            )
        except ValueError:
            # Unknown model in pricing table — log but don't blow up the apply.
            logger.exception("[apply] unknown model in pricing: %s", model)
            cost_cents = 0

        attempts.end(
            attempt_id,
            status=status,
            reason=reason,
            prompt_tokens=prompt,
            completion_tokens=completion,
            turn_count=turn_count,
            tool_call_counts=tool_call_counts,
        )
        usage.record(
            user_id=user_id,
            event_type="apply",
            cost_cents=cost_cents,
            metadata={
                "model": model,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "cached_tokens": cached,
                "cache_write_tokens": written,
                # What OpenRouter says it actually billed us (USD) — ground
                # truth to audit our computed cost_cents against.
                "openrouter_cost_usd": actual_cost_usd,
                "browser_mb": browser_mb,
                "test_mode": test_mode,
                "attempt_id": attempt_id,
                "application_id": application_id,
                "reason": reason,
            },
        )
        apps.release(application_id, status=status, reason=reason)
        return {
            "status": status,
            "application_id": application_id,
            "attempt_id": attempt_id,
            "cost_cents": cost_cents,
            "tokens": tokens,
            "reason": reason,
        }

    try:
        # 4. job row.
        job = jobs.get(job_id)
        if job is None or not getattr(job, "active", True):
            return _finish("skipped", "job_missing_or_inactive")

        # 5. profile (best-effort — never block the apply for lack of it).
        profile = profiles.get(user_id)
        profile_md = _build_profile_text(profile)

        # 6. tailored resume + PDF. Missing artifacts are a STATE problem —
        # this row should never have been apply-claimable — not an apply
        # failure. Requeue to 'pending' so the tailor worker produces the
        # resume. Releasing to 'failed' kept the row claimable (CLAIMABLE_FROM
        # includes 'failed') and one such row burned 734 attempts May–June 2026.
        tailored_row = tailored.get(user_id, job_id)
        pdf_storage_path: str | None = (tailored_row or {}).get("pdf_storage_path")
        if tailored_row is None or not pdf_storage_path:
            reason = "no_tailored_resume" if tailored_row is None else "no_tailored_pdf"
            attempts.end(attempt_id, status="failed", reason=reason)
            apps.requeue(application_id, "pending", reason=reason)
            logger.info(
                "[apply] %s on app %s — requeued to pending for tailoring",
                reason, application_id,
            )
            return {
                "status": "requeued",
                "application_id": application_id,
                "attempt_id": attempt_id,
                "reason": reason,
            }

        try:
            pdf_bytes = sb.storage.from_("resumes").download(pdf_storage_path)
        except Exception as exc:  # noqa: BLE001
            return _finish("failed", f"pdf_download_failed: {type(exc).__name__}: {exc}")

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fp:
            fp.write(pdf_bytes)
            tmp_pdf_path = fp.name

        resume_tex = tailored_row.get("latex_source") or ""
        tailor_metadata = tailored_row.get("tailor_metadata") or {}
        try:
            metadata_json = json.dumps(tailor_metadata)
        except (TypeError, ValueError):
            metadata_json = "{}"

        # 7. drive the tool-use loop. Route to the real application form, not a
        # company-careers wrapper (Stripe's gh_jid search page) — see
        # preferred_apply_url.
        result = run_apply(
            job_id=job.id,
            company=job.company,
            title=job.title,
            job_url=preferred_apply_url(job.id, job.url),
            resume_pdf_path=tmp_pdf_path,
            profile_md=profile_md,
            resume_tex=resume_tex,
            tailor_metadata_json=metadata_json,
            model=model,
            test_mode=test_mode,
        )

        status_raw = result.get("status", "failed")
        if status_raw == "infra_error":
            # Account/provider-wide failure inside the tool-use loop (LLM
            # credits/outage, Bright Data refusing the CDP connect): requeue to
            # 'tailored' (re-appliable) and signal the worker to back off rather
            # than burn the application to terminal 'failed'. Close the attempt
            # row with an 'infra:' reason — these rows don't count toward the
            # MAX_APPLY_ATTEMPTS cap (see count_for_application) and leaving
            # them 'pending' forever polluted the table (106 rows by July 2026).
            note = result.get("note") or "apply infra error"
            attempts.end(attempt_id, status="failed", reason=f"infra: {note}")
            apps.requeue(application_id, "tailored", reason=f"infra: {note}")
            logger.warning("[apply] infra error on app %s, requeued: %s", application_id, note)
            raise TransientInfraError(f"apply: {note}")
        # The runner emits a couple of values not in our terminal set ("lost_race"
        # never originates here; "gated:*" notes come back on `status='skipped'`).
        status = status_raw if status_raw in {"applied", "skipped", "failed"} else "failed"
        note = result.get("note") or None
        # Some models (Haiku 4.5, observed 2026-07-10) put the gated/skipped
        # string in report_done's STATUS field and plain English in note.
        # Without this it coerces to 'failed' → re-claimable → paid retries.
        if status == "failed" and status_raw.startswith(("gated:", "skipped:")):
            status = "skipped"
            note = f"{status_raw} | {note}" if note else status_raw
        # A gated/skipped verdict is terminal by definition. The runner
        # sometimes returns one as 'failed' (e.g. it filled the form but a
        # captcha wall blocked the final submit). Leaving it 'failed' is a bug:
        # 'failed' is re-claimable, so the worker re-fills the whole form with
        # paid LLM calls every cycle. Coerce to terminal 'skipped'.
        if status == "failed" and note and (
            note.startswith("gated:") or note.startswith("skipped:")
        ):
            status = "skipped"
        # A MAX_TURNS burnout is terminal on the FIRST hit: retrying the same
        # form with the same model fails identically, and each retry is a full
        # paid run (one application burned $3.30 across three 40-turn retries,
        # July 2026). Flip the model or the form handling before re-trying.
        if status == "failed" and note and note.startswith("hit MAX_TURNS"):
            status = "skipped"
            note = f"max_turns_exhausted: {note}"
            logger.warning(
                "[apply] app %s hit MAX_TURNS — terminal skip, no retries",
                application_id,
            )
        # Hard backstop: no failure class should retry unbounded. After
        # MAX_APPLY_ATTEMPTS attempts on this application, force it terminal
        # regardless of reason. (This attempt is already counted; infra-reason
        # attempts are excluded — transient outages say nothing about the job.)
        if status == "failed":
            prior = attempts.count_for_application(application_id)
            if prior >= MAX_APPLY_ATTEMPTS:
                status = "skipped"
                note = f"max_attempts_exhausted ({prior}): {note or 'repeated failure'}"
                logger.warning(
                    "[apply] app %s hit attempt cap (%d) — forcing terminal skipped",
                    application_id, prior,
                )
        _propagate_job_gate(jobs, job_id, status, note)
        return _finish(
            status,
            note,
            tokens=result.get("tokens"),
            browser_mb=float(result.get("browser_mb", 1.5)),
            turn_count=result.get("turn_count"),
            tool_call_counts=result.get("tool_call_counts") or None,
            actual_cost_usd=result.get("actual_cost_usd"),
        )

    except TransientInfraError:
        # Already requeued above; propagate so the worker backs off globally
        # instead of letting the broad handler mark this row terminal.
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("[apply] crashed for app %s: %s", application_id, exc)
        return _finish("failed", f"runner_crash: {type(exc).__name__}: {exc}")
    finally:
        if tmp_pdf_path:
            try:
                Path(tmp_pdf_path).unlink(missing_ok=True)
            except OSError:
                logger.warning("[apply] failed to clean up %s", tmp_pdf_path)
