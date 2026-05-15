"""Matchmaker worker.

For each user with a profile + master resume, finds classified active jobs that
the user hasn't been matched against yet, runs the matcher, and creates
applications rows:
  - decision='accept' or 'borderline' → applications.status='pending'
  - decision='reject'                  → applications.status='skipped'
                                          (reason prefixed 'matcher:')

This is the cost-saving stage. It runs the cheap Haiku matcher (~$0.001/job)
once per (user, job) instead of letting tailor + apply burn $0.27 per dud.

Entry: `python -m applyd.worker.matchmaker`
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from typing import Any

from ..classify import match_user_to_job
from ..config import load_env
from ..db import (
    ApplicationsRepo,
    UserProfilesRepo,
    UserResumesRepo,
    UsageEventsRepo,
    cost_cents_for_tailor,
    get_client,
)


logger = logging.getLogger("applyd.matchmaker")
_stop = False


def _on_signal(signum: int, _frame: Any) -> None:
    global _stop
    _stop = True
    logger.info("matchmaker: caught signal %s, draining…", signum)


def _candidate_jobs_for_user(sb, user_id: str, batch: int = 50) -> list[dict]:
    """Return classified, active, ungated jobs the user has no application row for.

    Uses a NOT EXISTS via PostgREST: it's awkward but doable — we fetch a batch
    of candidates, then filter out the ones the user already has an app row for.
    """
    already_app = (
        sb.table("applications").select("job_id")
        .eq("user_id", user_id).execute().data
    )
    seen_ids = {row["job_id"] for row in already_app if row["job_id"]}

    # Pull a batch of classified, active, ungated jobs in deterministic order.
    res = (
        sb.table("jobs")
        .select("id, title, classification")
        .eq("active", True)
        .is_("apply_gate", "null")
        .not_.is_("classification", "null")
        .order("posted_at", desc=True)
        .limit(batch * 4)  # over-fetch to allow filtering out seen ones
        .execute()
    )
    return [r for r in res.data if r["id"] not in seen_ids][:batch]


def _record_usage(usage: UsageEventsRepo, user_id: str, match: dict[str, Any], job_id: str) -> int:
    """Cost the match call against the user. Returns cents."""
    u = match.get("_usage", {})
    cost = cost_cents_for_tailor(
        model="claude-haiku-4-5",
        prompt_tokens=u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
        completion_tokens=u.get("output_tokens", 0),
        cached_tokens=u.get("cache_read_input_tokens", 0),
    )
    usage.record(
        user_id=user_id,
        event_type="enrich",  # closest existing event_type bucket; matcher is pre-application
        cost_cents=cost,
        metadata={
            "subtype": "match",
            "job_id": job_id,
            "decision": match.get("decision"),
            "confidence": match.get("confidence"),
            "input_tokens": u.get("input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0),
            "cached_tokens": u.get("cache_read_input_tokens", 0),
        },
    )
    return cost


def match_for_user(user_id: str, batch_limit: int = 50) -> dict[str, int]:
    """Run one pass of matching for one user. Returns counts."""
    sb = get_client()
    profiles = UserProfilesRepo(sb)
    resumes = UserResumesRepo(sb)
    apps = ApplicationsRepo(sb)
    usage = UsageEventsRepo(sb)

    profile = profiles.get(user_id)
    if not profile or not (profile.get("profile_answers") or "").strip():
        logger.info("matchmaker: user %s has no profile_answers, skipping", user_id)
        return {"accepted": 0, "rejected": 0, "borderline": 0, "matcher_cost_cents": 0}

    resume = resumes.get(user_id)
    if not resume or not (resume.get("latex_source") or "").strip():
        logger.info("matchmaker: user %s has no master resume, skipping", user_id)
        return {"accepted": 0, "rejected": 0, "borderline": 0, "matcher_cost_cents": 0}

    candidates = _candidate_jobs_for_user(sb, user_id, batch=batch_limit)
    if not candidates:
        return {"accepted": 0, "rejected": 0, "borderline": 0, "matcher_cost_cents": 0}

    counts = {"accepted": 0, "rejected": 0, "borderline": 0, "matcher_cost_cents": 0}

    for job in candidates:
        if _stop:
            break
        try:
            decision_obj = match_user_to_job(
                profile_answers=profile["profile_answers"],
                resume_latex=resume["latex_source"],
                classification=job["classification"],
            )
        except Exception:
            logger.exception("matchmaker: match call failed for job %s", job["id"])
            continue

        cost = _record_usage(usage, user_id, decision_obj, job["id"])
        counts["matcher_cost_cents"] += cost

        decision = decision_obj["decision"]
        reason = "matcher:" + (decision_obj.get("reason") or "")[:300]

        if decision == "reject":
            # Create the application row as skipped — audit trail.
            sb.table("applications").upsert(
                {
                    "user_id": user_id,
                    "job_id": job["id"],
                    "status": "skipped",
                    "reason": reason,
                },
                on_conflict="user_id,job_id",
            ).execute()
            counts["rejected"] += 1
        else:
            apps.upsert_pending(user_id, job["id"])
            if decision == "borderline":
                counts["borderline"] += 1
            else:
                counts["accepted"] += 1

    logger.info(
        "matchmaker: user=%s accepted=%d rejected=%d borderline=%d cost=%d¢",
        user_id, counts["accepted"], counts["rejected"], counts["borderline"], counts["matcher_cost_cents"],
    )
    return counts


def tick_once(batch_limit: int = 50) -> int:
    """One sweep: find all users with profile+resume, run match for each. Returns total counts."""
    sb = get_client()
    # Find candidate users: those with non-empty profile_answers.
    users = (
        sb.table("user_profiles").select("id, profile_answers")
        .not_.is_("profile_answers", "null").execute().data
    )
    users = [u for u in users if (u.get("profile_answers") or "").strip()]
    total = 0
    for u in users:
        if _stop:
            break
        counts = match_for_user(u["id"], batch_limit=batch_limit)
        total += counts["accepted"] + counts["rejected"] + counts["borderline"]
    return total


def run_forever(poll_seconds: int = 300, batch_limit: int = 50) -> None:
    """Long-running matchmaker. Polls every N seconds."""
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    logger.info("matchmaker: starting, poll=%ds batch=%d", poll_seconds, batch_limit)
    while not _stop:
        try:
            tick_once(batch_limit=batch_limit)
        except Exception:
            logger.exception("matchmaker: tick failed")
        for _ in range(poll_seconds):
            if _stop:
                break
            time.sleep(1)
    logger.info("matchmaker: stopped")


if __name__ == "__main__":
    poll = int(os.environ.get("APPLYD_MATCHMAKER_POLL", "300"))
    batch = int(os.environ.get("APPLYD_MATCHMAKER_BATCH", "50"))
    run_forever(poll_seconds=poll, batch_limit=batch)
