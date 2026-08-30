"""Run a small, sequential, test-mode application matrix across ATS sources.

This is intentionally a developer harness rather than a production batch
command. It never passes ``--test-mode false`` and stops testing an ATS after
the first non-review result so one broken form cannot burn the whole matrix.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from applyd.local_store import get_local_store


MATRIX = {
    "ashby": [
        "simplifyjobs:3029721d-d2a5-4aba-9f0b-3fca2d03b9f5",  # Julius AI
        "simplifyjobs:d7cea207-2957-4628-96f0-d1470db7594c",  # Flow Engineering
    ],
    "greenhouse": [
        "simplifyjobs:56133857-e632-4fbe-846d-376be2620735",  # Geotab
        "simplifyjobs:48921fdc-0754-40c8-91cc-f5e1bb6277bb",  # NewsBreak
    ],
    "lever": [
        "simplifyjobs:8c444c74-c1a6-46ae-95a3-eccb1ea203df",  # ShyftLabs
        "simplifyjobs:4cbc04d5-d651-438a-81ec-b82f768a6602",  # Palantir
    ],
    "workable": [
        "simplifyjobs:7374f199-7fc6-45ac-8316-dc868e97ee81",  # PlanetArt mobile
        "simplifyjobs:077176ea-84be-43ad-9204-26626feaa4df",  # PlanetArt web
    ],
}


def _run(args: list[str], env: dict[str, str]) -> int:
    return subprocess.run(args, env=env, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ats", action="append", choices=sorted(MATRIX))
    parser.add_argument("--jobs-per-ats", type=int, default=2, choices=(1, 2))
    parser.add_argument(
        "--job-number", type=int, choices=(1, 2),
        help="run only the first or second configured job for each selected ATS",
    )
    parser.add_argument("--skip-tailor", action="store_true")
    parser.add_argument("--report", default="tmp/ats-test-matrix.json")
    args = parser.parse_args()

    selected = args.ats or list(MATRIX)
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    env.setdefault("APPLYD_APPLY_MAX_TURNS", "50")
    python = sys.executable
    store = get_local_store()
    report: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "test_mode": True,
        "results": [],
    }

    for ats in selected:
        source_failed = False
        job_ids = (
            [MATRIX[ats][args.job_number - 1]]
            if args.job_number else MATRIX[ats][: args.jobs_per_ats]
        )
        for job_id in job_ids:
            if source_failed:
                report["results"].append({
                    "ats": ats, "job_id": job_id, "status": "not_run",
                    "reason": "source fail-fast",
                })
                continue
            job = store.get(job_id)
            print(f"\n=== {ats}: {job.company} - {job.title} ===", flush=True)
            if not args.skip_tailor:
                rc = _run([
                    python, "-m", "applyd.cli", "tailor", job_id,
                    "--resume", "resume.json", "--template", "resume_template.tex",
                    "--output", "out",
                ], env)
                if rc != 0:
                    report["results"].append({
                        "ats": ats, "job_id": job_id, "company": job.company,
                        "title": job.title, "status": "tailor_failed", "exit_code": rc,
                    })
                    source_failed = True
                    continue

            before = store.get_apply_attempts(job_id)
            rc = _run([
                python, "-m", "applyd.cli", "apply", job_id,
                "--test-mode", "true", "--browser", "local",
            ], env)
            attempts = store.get_apply_attempts(job_id)
            attempt = attempts[-1] if len(attempts) > len(before) else {}
            status = str(attempt.get("status") or "missing_attempt")
            reason = str(attempt.get("reason") or "")
            passed = status == "review" and reason == "test_mode:ready_for_review"
            report["results"].append({
                "ats": ats,
                "job_id": job_id,
                "company": job.company,
                "title": job.title,
                "status": status,
                "reason": reason,
                "cost_usd": float(attempt.get("cost_usd") or 0),
                "turn_count": int(attempt.get("turn_count") or 0),
                "tool_calls": attempt.get("tool_calls") or {},
                "exit_code": rc,
                "passed": passed,
            })
            source_failed = not passed

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    output = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    passed = sum(bool(row.get("passed")) for row in report["results"])
    ran = sum(row.get("status") != "not_run" for row in report["results"])
    cost = sum(float(row.get("cost_usd") or 0) for row in report["results"])
    print(f"\nMatrix complete: passed={passed}/{ran} recorded_apply_cost=${cost:.4f}")
    print(f"Report: {output}")
    return 0 if passed == ran else 1


if __name__ == "__main__":
    raise SystemExit(main())
