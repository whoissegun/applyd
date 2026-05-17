#!/usr/bin/env python3
"""Manual catalog pipeline runner.

Examples:
  python scripts/manual_pipeline.py discover
  python scripts/manual_pipeline.py enrich --enrich-workers 12
  python scripts/manual_pipeline.py score --score-workers 4
  python scripts/manual_pipeline.py all --enrich-workers 12 --score-workers 4
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


def _run(cmd: Sequence[str]) -> int:
    print("+ " + " ".join(cmd), flush=True)
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC) if not existing else f"{SRC}{os.pathsep}{existing}"
    return subprocess.run(cmd, check=False, cwd=ROOT, env=env).returncode


def run_discover(args: argparse.Namespace) -> int:
    cmd = [
        sys.executable,
        "-m",
        "applyd",
        "discover",
        "--targets",
        args.targets,
        "--cache",
        args.cache,
        "--broad-cache",
        args.broad_cache,
    ]
    if args.search_provider:
        cmd.extend(["--search-provider", args.search_provider])
    if args.no_broad:
        cmd.append("--no-broad")
    return _run(cmd)


def run_enrich(args: argparse.Namespace) -> int:
    cmd = [
        sys.executable,
        "-m",
        "applyd",
        "enrich",
        "--workers",
        str(args.enrich_workers),
        "--save-every",
        str(args.save_every),
    ]
    if args.enrich_limit:
        cmd.extend(["--limit", str(args.enrich_limit)])
    if args.source:
        cmd.extend(["--source", args.source])
    if args.retry_failed:
        cmd.append("--retry-failed")
    if args.dry_run:
        cmd.append("--dry-run")
    return _run(cmd)


def _scorable_users() -> list[str]:
    from applyd.config import load_env
    from applyd.db import get_client

    load_env()
    rows = (
        get_client()
        .table("user_profiles")
        .select("id, profile_answers")
        .not_.is_("profile_answers", "null")
        .execute()
        .data
    )
    return [r["id"] for r in rows if (r.get("profile_answers") or "").strip()]


def _score_user(user_id: str, batch_limit: int) -> tuple[str, int]:
    cmd = [
        sys.executable,
        "-m",
        "applyd.worker.matchmaker",
        "--once",
        "--user",
        user_id,
        "--batch-limit",
        str(batch_limit),
    ]
    return user_id, _run(cmd)


def run_score(args: argparse.Namespace) -> int:
    users = [args.user] if args.user else _scorable_users()
    if not users:
        print("No users with profile_answers to score.", file=sys.stderr)
        return 0

    workers = max(1, min(args.score_workers, len(users)))
    print(f"Scoring {len(users)} user(s) with {workers} process(es).", flush=True)

    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_score_user, user_id, args.score_batch_limit)
            for user_id in users
        ]
        for fut in concurrent.futures.as_completed(futures):
            user_id, code = fut.result()
            if code:
                failures += 1
                print(f"score failed for user={user_id} exit={code}", file=sys.stderr)

    return 1 if failures else 0


def run_all(args: argparse.Namespace) -> int:
    for step in (run_discover, run_enrich, run_score):
        code = step(args)
        if code:
            return code
    return 0


def add_shared_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--targets", default="targets.json")
    parser.add_argument("--cache", default="data/resolver_cache.json")
    parser.add_argument("--broad-cache", default="data/broad_search_cache.json")
    parser.add_argument("--search-provider", choices=["brave", "serper"])
    parser.add_argument("--no-broad", action="store_true")
    parser.add_argument("--enrich-workers", type=int, default=8)
    parser.add_argument("--enrich-limit", type=int, default=0)
    parser.add_argument("--source")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--save-every", type=int, default=25)
    parser.add_argument("--score-workers", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--score-batch-limit", type=int, default=50)
    parser.add_argument("--user", help="score one user UUID")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="manual discover/enrich/score runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, func in (
        ("discover", run_discover),
        ("enrich", run_enrich),
        ("score", run_score),
        ("all", run_all),
    ):
        p = sub.add_parser(name)
        add_shared_args(p)
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
