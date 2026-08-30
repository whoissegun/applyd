from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from ..config import load_env
from ..discovery.routing import detect_ats, preferred_apply_url
from ..liveness import check_jobs_liveness
from ..local_store import get_local_store
from .apply import cmd_apply
from .tailor import cmd_tailor


MANUAL_ONLY_ATS = {"smartrecruiters"}


def _normalized_title(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def _similar_title(left: str, right: str) -> bool:
    a, b = _normalized_title(left), _normalized_title(right)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.82


def _matches_prior_role(
    company: str, title: str, prior_roles: list[tuple[str, str]],
) -> bool:
    company_key = company.casefold()
    return any(
        company_key == prior_company and _similar_title(title, prior_title)
        for prior_company, prior_title in prior_roles
    )


def _captcha_gate(attempt: dict[str, Any] | None) -> bool:
    if not attempt:
        return False
    reason = str(attempt.get("reason") or "").strip().lower()
    return attempt.get("status") in {"review", "skipped"} and reason.startswith(
        "gated:captcha"
    )


def _failure_category(attempt: dict[str, Any] | None) -> str | None:
    if not attempt or attempt.get("status") == "applied":
        return None
    reason = str(attempt.get("reason") or "").lower()
    if reason.startswith("gated:captcha"):
        return "captcha"
    if "repeated_tool_failure" in reason:
        return "tool_failure"
    if attempt.get("status") in {"failed", "infra_error"}:
        return "runtime_failure"
    return None


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    temp.replace(path)


def _latest_attempt(store, job_id: str) -> dict[str, Any] | None:
    attempts = store.get_apply_attempts(job_id)
    return attempts[-1] if attempts else None


def _primary_browser_provider(ats: str, *, test_mode: bool) -> str:
    return "brightdata" if ats == "lever" and not test_mode else "local"


def _ats_failure_total(failures: Counter[tuple[str, str]], ats: str) -> int:
    return sum(
        count
        for (failing_ats, _category), count in failures.items()
        if failing_ats == ats
    )


def cmd_apply_batch(args: argparse.Namespace) -> int:
    """Tailor and apply serially, with one CAPTCHA-only Bright Data fallback."""
    load_env()
    store = get_local_store()
    ranked = list(
        store.iter_ranked_matches(
            limit=max(200, args.top * 20), minimum_score=args.minimum_score
        )
    )
    if not ranked:
        print("✗ no ranked matches; run `applyd match` first", file=sys.stderr)
        return 2

    # Protect every prior attempt, including a duplicate record for a closely
    # related requisition at the same company. Cross-source duplicates have
    # different job IDs, so checking only the current ID can repeat paid work
    # (and, for successful attempts, produce duplicate submissions).
    prior_roles: list[tuple[str, str]] = []
    for row in ranked:
        app = store.get_application_by_job(row["job_id"])
        app_reason = str((app or {}).get("reason") or "")
        if (
            app
            and (
                app.get("status") == "applied"
                or app_reason.startswith("manual_only_ats:")
            )
        ) or store.get_apply_attempts(row["job_id"]):
            prior_roles.append(
                (str(row["company"]).casefold(), str(row["title"]))
            )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = Path(args.report or f"data/batches/apply-{stamp}.json")
    report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "requested": args.top,
        "test_mode": bool(args.test_mode),
        "local_browser": "local",
        "captcha_fallback": args.captcha_fallback,
        "results": [],
    }
    _write_report(report_path, report)

    attempted = 0
    ats_selected: Counter[str] = Counter()
    ats_failures: Counter[tuple[str, str]] = Counter()
    used_companies: set[str] = set()
    apply_cost = 0.0

    for row in ranked:
        if attempted >= args.top or (
            args.max_total_apply_cost_usd > 0
            and apply_cost >= args.max_total_apply_cost_usd
        ):
            break
        job_id = row["job_id"]
        company_key = str(row["company"]).casefold()
        if company_key in used_companies:
            continue
        if _matches_prior_role(str(row["company"]), str(row["title"]), prior_roles):
            continue
        if store.get_apply_attempts(job_id):
            continue
        application = store.get_application_by_job(job_id)
        if application:
            status = application.get("status")
            reason = str(application.get("reason") or "")
            if status in {"applied", "in_progress"} or (
                status == "review" and reason.startswith("manual_only_ats:")
            ):
                continue
        job = store.get(job_id)
        if job is None:
            continue
        apply_url = preferred_apply_url(job.id, job.url, company=job.company)
        ats = detect_ats(apply_url) or "unknown"
        if ats_selected[ats] >= args.max_per_ats:
            continue
        if _ats_failure_total(ats_failures, ats) >= args.ats_failure_limit:
            continue

        live = check_jobs_liveness([job], workers=1)[0]
        store.record_liveness(job.id, live.status, live.method, live.detail)
        if live.status == "closed":
            report["results"].append({
                "job_id": job_id, "company": job.company, "title": job.title,
                "ats": ats, "score": row["score"], "status": "posting_closed",
            })
            _write_report(report_path, report)
            continue

        if ats in MANUAL_ONLY_ATS:
            reason = f"manual_only_ats:{ats}"
            store.mark_application_review(job.id, reason)
            used_companies.add(company_key)
            ats_selected[ats] += 1
            attempted += 1
            print(
                f"\n══ batch {attempted}/{args.top} | {ats} | "
                f"score={row['score']:.1f} | {job.company} — {job.title} ══",
                file=sys.stderr,
                flush=True,
            )
            report["results"].append({
                "job_id": job_id,
                "company": job.company,
                "title": job.title,
                "ats": ats,
                "score": row["score"],
                "final_status": "review",
                "final_reason": reason,
                "apply_cost_usd": 0.0,
                "cost_warning": False,
            })
            report["ats_selected"] = dict(ats_selected)
            _write_report(report_path, report)
            print(
                f"  ↳ {ats} is manual-only after unsuccessful pilot evidence; "
                "queued for review at $0 model cost",
                file=sys.stderr,
                flush=True,
            )
            continue

        used_companies.add(company_key)
        ats_selected[ats] += 1
        print(
            f"\n══ batch {attempted + 1}/{args.top} | {ats} | "
            f"score={row['score']:.1f} | {job.company} — {job.title} ══",
            file=sys.stderr,
            flush=True,
        )

        application = store.get_application_by_job(job_id)
        if not application or not application.get("pdf_path"):
            try:
                tailor_code = cmd_tailor(argparse.Namespace(
                    job_id=job_id,
                    resume=args.resume,
                    template=args.template,
                    output=args.output,
                    model=args.tailor_model,
                    force=False,
                    no_compile=False,
                ))
            except Exception as exc:
                reason = f"{type(exc).__name__}: {str(exc)[:500]}"
                report["results"].append({
                    "job_id": job_id,
                    "company": job.company,
                    "title": job.title,
                    "ats": ats,
                    "score": row["score"],
                    "status": "tailor_provider_error",
                    "reason": reason,
                })
                report["paused"] = True
                report["pause_reason"] = reason
                _write_report(report_path, report)
                print(
                    f"✗ tailoring provider error; batch paused safely: {reason}",
                    file=sys.stderr,
                )
                break
            if tailor_code != 0:
                report["results"].append({
                    "job_id": job_id, "company": job.company, "title": job.title,
                    "ats": ats, "score": row["score"], "status": "tailor_failed",
                })
                _write_report(report_path, report)
                continue

        attempted += 1
        common = dict(
            job_id=job_id,
            model=args.apply_model,
            test_mode=args.test_mode,
            liveness_ttl_minutes=0,
            max_cost_usd=args.max_cost_usd,
        )
        primary_provider = _primary_browser_provider(ats, test_mode=args.test_mode)
        if primary_provider == "brightdata":
            print(
                "  ↳ Lever route: starting directly with Bright Data",
                file=sys.stderr,
                flush=True,
            )
        try:
            cmd_apply(argparse.Namespace(browser=primary_provider, **common))
        except Exception as exc:  # one job must not kill the batch
            print(
                f"✗ {primary_provider} attempt raised {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        primary_attempt = _latest_attempt(store, job_id)
        if primary_attempt:
            apply_cost += float(primary_attempt.get("cost_usd") or 0)
        local_attempt = primary_attempt if primary_provider == "local" else None
        primary_brightdata_attempt = (
            primary_attempt if primary_provider == "brightdata" else None
        )

        fallback_attempt = None
        if (
            not args.test_mode
            and args.captcha_fallback == "brightdata"
            and _captcha_gate(local_attempt)
        ):
            print(
                "  ↳ local CAPTCHA gate confirmed; restarting once with Bright Data",
                file=sys.stderr,
                flush=True,
            )
            try:
                cmd_apply(argparse.Namespace(browser="brightdata", **common))
            except Exception as exc:
                print(
                    f"✗ Bright Data fallback raised {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            fallback_attempt = _latest_attempt(store, job_id)
            if fallback_attempt and (
                not local_attempt or fallback_attempt.get("id") != local_attempt.get("id")
            ):
                apply_cost += float(fallback_attempt.get("cost_usd") or 0)

        brightdata_attempt = primary_brightdata_attempt or fallback_attempt
        final_attempt = brightdata_attempt or local_attempt
        category = _failure_category(final_attempt)
        if category:
            ats_failures[(ats, category)] += 1
        result_row = {
            "job_id": job_id,
            "company": job.company,
            "title": job.title,
            "ats": ats,
            "score": row["score"],
            "local": local_attempt,
            "brightdata": brightdata_attempt,
            "final_status": (final_attempt or {}).get("status", "unknown"),
            "final_reason": (final_attempt or {}).get("reason", "no attempt persisted"),
        }
        job_apply_cost = sum(
            float(attempt.get("cost_usd") or 0)
            for attempt in (local_attempt, brightdata_attempt)
            if attempt
        )
        result_row["apply_cost_usd"] = job_apply_cost
        result_row["cost_warning"] = job_apply_cost > 0.10
        if result_row["cost_warning"]:
            print(
                f"⚠ application model cost exceeded $0.10: ${job_apply_cost:.4f}",
                file=sys.stderr,
            )
        report["results"].append(result_row)
        report["apply_cost_usd"] = apply_cost
        report["ats_selected"] = dict(ats_selected)
        report["ats_failures"] = {
            f"{ats_name}:{category_name}": count
            for (ats_name, category_name), count in ats_failures.items()
        }
        _write_report(report_path, report)

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["attempted"] = attempted
    report["apply_cost_usd"] = apply_cost
    _write_report(report_path, report)
    statuses = Counter(item.get("final_status", item.get("status", "unknown")) for item in report["results"])
    print(
        f"\n✓ batch finished: attempted={attempted}/{args.top} "
        f"apply_cost=${apply_cost:.4f} statuses={dict(statuses)} "
        f"report={report_path}",
        file=sys.stderr,
    )
    return 0 if attempted else 2
