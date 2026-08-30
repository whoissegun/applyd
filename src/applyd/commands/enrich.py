from __future__ import annotations

import argparse
import sys
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from typing import Optional

import httpx

from ..config import load_env
from ..enrichment import MIN_USEFUL_CHARS, fetch_text, local_browser_retriever
from ..enrichment.extract import JobExtractor
from ..discovery.routing import parse_ats_url
from ..discovery import ATS_MODULES
from ..local_store import get_local_store
from ..models import Job


def _is_credit_budget_error(exc: BaseException) -> bool:
    if getattr(exc, "status_code", None) == 402:
        return True
    text = str(exc).casefold()
    return "in_flight_budget_exhausted" in text or "requires more credits" in text


def _retrieval_url(job: Job) -> str:
    return str(job.raw.get("_applyd_retrieval_url") or job.url)


def cmd_enrich(args: argparse.Namespace) -> int:
    """Retrieve descriptions for free, then extract structured facts with Kimi."""
    load_env()
    repo = get_local_store()
    extract_only = bool(getattr(args, "classify_backfill", False))
    use_browser = not bool(getattr(args, "no_browser", False))
    use_extractor = not bool(getattr(args, "no_extract", False))

    candidates: list[Job] = []
    source_iter = (
        repo.iter_unclassified()
        if extract_only
        else repo.iter_pending_enrichment(
            include_failed=args.retry_failed,
            source=args.source,
        )
    )
    for job in source_iter:
        if args.source and job.source != args.source:
            continue
        if not job.url and not extract_only:
            continue
        if extract_only and (
            not job.description or len(job.description) < MIN_USEFUL_CHARS
        ):
            continue
        candidates.append(job)
        if args.limit and len(candidates) >= args.limit:
            break

    mode = "extract" if extract_only else "retrieve+extract"
    print(f"→ {len(candidates)} jobs to {mode}", file=sys.stderr)
    if args.dry_run:
        by_source: dict[str, int] = {}
        for job in candidates:
            by_source[job.source] = by_source.get(job.source, 0) + 1
        for source, count in sorted(by_source.items(), key=lambda item: -item[1]):
            print(f"  {source:16s} {count}", file=sys.stderr)
        return 0

    described: list[Job] = []
    retrieval_stats = {
        "ats": 0, "http": 0, "browser": 0, "unsupported": 0, "failed": 0
    }

    if extract_only:
        described = candidates
    else:
        workers = max(1, args.workers)
        board_cache: dict = {}
        limits = httpx.Limits(
            max_connections=workers * 3,
            max_keepalive_connections=workers * 2,
        )
        failed: list[tuple[Job, str | None]] = []
        with httpx.Client(
            timeout=60.0,
            headers={"User-Agent": "applyd/0.1"},
            limits=limits,
        ) as client:

            def retrieve(job: Job) -> tuple[Job, str, str, Optional[str]]:
                try:
                    text, tier, error = fetch_text(
                        _retrieval_url(job),
                        client=client,
                        board_cache=board_cache,
                    )
                    return job, text, tier, error
                except Exception as exc:  # noqa: BLE001
                    return job, "", "failed", f"{type(exc).__name__}: {exc}"

            with ThreadPoolExecutor(max_workers=workers) as pool:
                # ATS list endpoints return an entire company board. Pre-fetch
                # each unique board exactly once so per-job workers never race
                # to download the same board repeatedly.
                board_routes: dict[tuple[str, str], object] = {}
                for job in candidates:
                    parsed = parse_ats_url(_retrieval_url(job))
                    if parsed and parsed[0] in ATS_MODULES:
                        ats, company, _job_id = parsed
                        board_routes[(ats, company)] = ATS_MODULES[ats]
                board_futures = {}
                for key, module in board_routes.items():
                    company = key[1]
                    future = pool.submit(module.fetch, company, client=client)
                    board_futures[future] = key
                for future in as_completed(board_futures):
                    key = board_futures[future]
                    try:
                        board_cache[key] = future.result()
                    except Exception:
                        board_cache[key] = []

                futures = [pool.submit(retrieve, job) for job in candidates]
                for future in as_completed(futures):
                    job, text, tier, error = future.result()
                    if text and len(text) >= MIN_USEFUL_CHARS:
                        job.description = text
                        repo.mark_enriched(job.id, text, tier, error)
                        described.append(job)
                        retrieval_stats[tier] = retrieval_stats.get(tier, 0) + 1
                    else:
                        failed.append((job, error))

        browser_candidates: list[tuple[Job, str | None]] = []
        unsupported_failures: list[tuple[Job, str | None]] = []
        for job, error in failed:
            parsed = parse_ats_url(_retrieval_url(job))
            if (parsed and parsed[0] in ATS_MODULES) or job.source in ATS_MODULES:
                browser_candidates.append((job, error))
            else:
                unsupported_failures.append((job, error))
                repo.mark_enriched(
                    job.id, job.description, "unsupported_ats",
                    "ATS is outside applyd's supported application platforms",
                )
                retrieval_stats["unsupported"] += 1

        if browser_candidates and use_browser:
            print(
                f"  → {len(browser_candidates)} supported-ATS misses; "
                "trying persistent local Chrome",
                file=sys.stderr,
            )
            try:
                with local_browser_retriever() as browser:
                    for job, prior_error in browser_candidates:
                        text = browser.fetch(job.url)
                        if text:
                            job.description = text
                            repo.mark_enriched(job.id, text, "browser", None)
                            described.append(job)
                            retrieval_stats["browser"] += 1
                        else:
                            repo.mark_enriched(
                                job.id,
                                job.description,
                                "failed",
                                prior_error or "local browser returned no useful text",
                            )
                            retrieval_stats["failed"] += 1
            except Exception as exc:  # noqa: BLE001
                message = f"local browser unavailable: {type(exc).__name__}: {exc}"
                print(f"  ⚠ {message}", file=sys.stderr)
                for job, prior_error in browser_candidates:
                    repo.mark_enriched(
                        job.id,
                        job.description,
                        "failed",
                        prior_error or message,
                    )
                    retrieval_stats["failed"] += 1
        else:
            for job, error in browser_candidates:
                repo.mark_enriched(job.id, job.description, "failed", error)
                retrieval_stats["failed"] += 1

    extraction_stats = {"ok": 0, "failed": 0, "requests": 0, "fallback": 0}
    extraction_cost = 0.0
    credit_exhausted = False
    if use_extractor and described:
        try:
            extractor = JobExtractor()
        except RuntimeError as exc:
            print(f"  ⚠ semantic extraction disabled: {exc}", file=sys.stderr)
            extractor = None
        if extractor is not None:
            workers = max(1, args.workers)
            batch_size = max(1, getattr(args, "batch_size", 5))

            def extract_batch(batch: list[Job]):
                if len(batch) == 1:
                    job = batch[0]
                    assert job.description is not None
                    result = extractor.extract(
                        job.title, job.company, job.description
                    )
                    return {job.id: result}, (), result.cost_usd
                result = extractor.extract_many(
                    [
                        (job.id, job.title, job.company, job.description or "")
                        for job in batch
                    ]
                )
                return result.results, result.missing_ids, result.cost_usd

            jobs_by_id = {job.id: job for job in described}
            work = deque(
                described[index:index + batch_size]
                for index in range(0, len(described), batch_size)
            )
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pending = {}
                while work and len(pending) < workers:
                    batch = work.popleft()
                    pending[pool.submit(extract_batch, batch)] = batch
                while pending:
                    completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in completed:
                        batch = pending.pop(future)
                        extraction_stats["requests"] += 1
                        try:
                            results, missing_ids, request_cost = future.result()
                            extraction_cost += request_cost
                            for job_id, result in results.items():
                                repo.set_classification(
                                    job_id,
                                    result.facts,
                                    model=result.model,
                                    cost_usd=result.cost_usd,
                                )
                                extraction_stats["ok"] += 1
                            for job_id in reversed(missing_ids):
                                work.appendleft([jobs_by_id[job_id]])
                                extraction_stats["fallback"] += 1
                        except Exception as exc:  # noqa: BLE001
                            if _is_credit_budget_error(exc):
                                credit_exhausted = True
                                extraction_stats["failed"] += len(batch)
                                print(
                                    "  ⚠ OpenRouter credit budget exhausted; "
                                    "stopping new extraction requests",
                                    file=sys.stderr,
                                )
                            elif len(batch) > 1:
                                # Preserve good isolation: a malformed/failed batch
                                # becomes individual retries instead of losing jobs.
                                for job in reversed(batch):
                                    work.appendleft([job])
                                    extraction_stats["fallback"] += 1
                            else:
                                extraction_stats["failed"] += 1
                                print(
                                    f"  ⚠ Kimi extraction failed: "
                                    f"{type(exc).__name__}: {str(exc)[:240]}",
                                    file=sys.stderr,
                                )
                    while work and len(pending) < workers and not credit_exhausted:
                        batch = work.popleft()
                        pending[pool.submit(extract_batch, batch)] = batch

    retrieval_summary = " ".join(
        f"{key}={value}" for key, value in retrieval_stats.items()
    )
    print(
        f"✓ enrichment complete: {retrieval_summary} "
        f"extracted={extraction_stats['ok']} extract-failed={extraction_stats['failed']} "
        f"requests={extraction_stats['requests']} fallback={extraction_stats['fallback']} "
        f"Kimi=${extraction_cost:.4f}",
        file=sys.stderr,
    )
    return 3 if credit_exhausted else 0
