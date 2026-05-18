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
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
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

    Pages through `jobs` ordered by `posted_at desc`, dropping anything the user
    already has an `applications` row for. Paging (rather than a single
    over-fetched window) is what keeps the matchmaker progressing once the
    user's seen set covers all the most-recent jobs.
    """
    already_app = (
        sb.table("applications").select("job_id")
        .eq("user_id", user_id).execute().data
    )
    seen_ids = {row["job_id"] for row in already_app if row["job_id"]}

    page_size = max(batch * 4, 100)
    max_scan = max(batch * 50, 2000)  # cap so a fully-saturated user doesn't full-scan jobs
    candidates: list[dict] = []
    offset = 0
    while len(candidates) < batch and offset < max_scan:
        res = (
            sb.table("jobs")
            .select("id, title, classification")
            .eq("active", True)
            .is_("apply_gate", "null")
            .not_.is_("classification", "null")
            .order("posted_at", desc=True)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            break
        for r in rows:
            if r["id"] not in seen_ids:
                candidates.append(r)
                if len(candidates) >= batch:
                    break
        offset += page_size
    return candidates[:batch]


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


def match_for_user(
    user_id: str,
    batch_limit: int = 15,
    workers: int = 8,
) -> dict[str, int]:
    """Run one pass of matching for one user. Returns counts.

    Match calls fan out across `workers` threads — Haiku latency dominates per
    call (~1–2s) so serial scoring stalls the whole worker-all loop.
    """
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
    if not resume or not (resume.get("resume_text") or "").strip():
        logger.info("matchmaker: user %s has no master resume, skipping", user_id)
        return {"accepted": 0, "rejected": 0, "borderline": 0, "matcher_cost_cents": 0}

    candidates = _candidate_jobs_for_user(sb, user_id, batch=batch_limit)
    if not candidates:
        return {"accepted": 0, "rejected": 0, "borderline": 0, "matcher_cost_cents": 0}

    counts = {"accepted": 0, "rejected": 0, "borderline": 0, "matcher_cost_cents": 0}
    profile_answers = profile["profile_answers"]
    resume_text = resume["resume_text"]

    def score(job: dict) -> tuple[dict, dict[str, Any] | None, Exception | None]:
        try:
            decision_obj = match_user_to_job(
                profile_answers=profile_answers,
                resume_text=resume_text,
                classification=job["classification"],
            )
            return job, decision_obj, None
        except Exception as exc:
            return job, None, exc

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(score, j) for j in candidates]
        try:
            for fut in as_completed(futures):
                if _stop:
                    for f in futures:
                        f.cancel()
                    break

                job, decision_obj, err = fut.result()
                if err is not None or decision_obj is None:
                    logger.exception(
                        "matchmaker: match call failed for job %s: %r",
                        job["id"],
                        err,
                    )
                    continue

                cost = _record_usage(usage, user_id, decision_obj, job["id"])
                counts["matcher_cost_cents"] += cost

                decision = decision_obj["decision"]
                reason = "matcher:" + (decision_obj.get("reason") or "")[:300]

                if decision == "reject":
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
        except KeyboardInterrupt:
            for f in futures:
                f.cancel()
            raise

    logger.info(
        "matchmaker: user=%s accepted=%d rejected=%d borderline=%d cost=%d¢",
        user_id, counts["accepted"], counts["rejected"], counts["borderline"], counts["matcher_cost_cents"],
    )
    return counts


def tick_once(batch_limit: int = 15, workers: int = 8) -> int:
    """One sweep: find all users with profile+resume, run match for each.
    Returns total scored pairs."""
    sb = get_client()
    users = (
        sb.table("user_profiles").select("id, profile_answers")
        .not_.is_("profile_answers", "null").execute().data
    )
    users = [u for u in users if (u.get("profile_answers") or "").strip()]
    total = 0
    for u in users:
        if _stop:
            break
        counts = match_for_user(u["id"], batch_limit=batch_limit, workers=workers)
        total += counts["accepted"] + counts["rejected"] + counts["borderline"]
    return total


def run_forever(
    poll_seconds: int = 300,
    batch_limit: int = 15,
    workers: int = 8,
) -> None:
    """Long-running matchmaker. Polls every N seconds."""
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    logger.info(
        "matchmaker: starting, poll=%ds batch=%d workers=%d",
        poll_seconds, batch_limit, workers,
    )
    while not _stop:
        try:
            tick_once(batch_limit=batch_limit, workers=workers)
        except Exception:
            logger.exception("matchmaker: tick failed")
        for _ in range(poll_seconds):
            if _stop:
                break
            time.sleep(1)
    logger.info("matchmaker: stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="applyd matchmaker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one scoring sweep and exit",
    )
    parser.add_argument(
        "--user",
        help="score one user UUID instead of sweeping every user",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=int(os.environ.get("APPLYD_MATCHMAKER_POLL", "300")),
        help="poll interval for long-running mode",
    )
    parser.add_argument(
        "--batch-limit",
        type=int,
        default=int(os.environ.get("APPLYD_MATCHMAKER_BATCH", "15")),
        help="max jobs to score per user per sweep",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("APPLYD_MATCHMAKER_WORKERS", "8")),
        help="thread pool size for Haiku match calls",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("APPLYD_MATCHMAKER_LOG_LEVEL", "INFO"),
        help="python logging level (DEBUG/INFO/WARNING)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(name)s %(message)s",
    )

    if args.once:
        if args.user:
            counts = match_for_user(
                args.user, batch_limit=args.batch_limit, workers=args.workers,
            )
            total = counts["accepted"] + counts["rejected"] + counts["borderline"]
        else:
            total = tick_once(batch_limit=args.batch_limit, workers=args.workers)
        logger.info("matchmaker: one-shot complete, scored=%d", total)
        return

    run_forever(
        poll_seconds=args.poll_seconds,
        batch_limit=args.batch_limit,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
