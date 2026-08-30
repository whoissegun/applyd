from __future__ import annotations

import argparse
import sys
from typing import Optional

from .commands import (
    cmd_apply,
    cmd_apply_batch,
    cmd_discover,
    cmd_dedupe,
    cmd_enrich,
    cmd_evaluate,
    cmd_init,
    cmd_import_resume,
    cmd_jobs,
    cmd_match,
    cmd_profile_gaps,
    cmd_resolve,
    cmd_tailor,
    cmd_trace,
    cmd_verify_live,
)
from .discovery import ATS_MODULES


SOURCES_AGGREGATORS = ["simplifyjobs", "broad_search"]
SOURCES_ATS = list(ATS_MODULES.keys())
ALL_SOURCES = SOURCES_AGGREGATORS + SOURCES_ATS


def _parse_bool(value: str) -> bool:
    value = value.strip().lower()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="applyd",
        description="Local-first autonomous job application engine",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="create the local SQLite database")
    p_init.add_argument("--db", default="data/applyd.sqlite3")
    p_init.add_argument("--profile", default="profile.json")
    p_init.add_argument(
        "--import-legacy",
        metavar="PATH",
        help="optionally import a legacy jobs.json catalog",
    )
    p_init.set_defaults(func=cmd_init)

    p_resume = sub.add_parser(
        "import-resume",
        help="convert a Jake-style LaTeX resume into canonical resume.json",
    )
    p_resume.add_argument("source", help="path to the source .tex resume")
    p_resume.add_argument("--profile", default="profile.json")
    p_resume.add_argument("--output", default="resume.json")
    p_resume.set_defaults(func=cmd_import_resume)

    p_disc = sub.add_parser("discover", help="pull from aggregators + user-specified companies")
    p_disc.add_argument("--targets", default="targets.json",
                        help="path to targets.json with {\"companies\": [...], \"broad_dorks\": [...]}")
    p_disc.add_argument("--cache", default="data/resolver_cache.json",
                        help="path to resolver cache (default: data/resolver_cache.json)")
    p_disc.add_argument("--broad-cache", default="data/broad_search_cache.json",
                        help="path to broad-search result cache (default: data/broad_search_cache.json)")
    p_disc.add_argument("--search-provider", choices=["brave", "serper"],
                        help="override SEARCH_PROVIDER env var")
    p_disc.add_argument("--no-broad", action="store_true",
                        help="skip the broad-search aggregator this run")
    p_disc.add_argument(
        "--limit", type=int, default=0,
        help="hard cap on jobs written this run (0 = unlimited)",
    )
    p_disc.add_argument(
        "--include-unsupported-ats",
        action="store_true",
        help="include jobs outside the ATS platforms applyd can automate",
    )
    p_disc.set_defaults(func=cmd_discover)

    p_jobs = sub.add_parser("jobs", help="query the shared jobs catalog")
    p_jobs.add_argument("--level", choices=["intern", "new_grad", "mid", "senior"])
    p_jobs.add_argument("--specialty",
                        help="ml | backend | frontend | fullstack | mobile | infra | data | security")
    p_jobs.add_argument("--location", help="substring match (e.g. 'new york', 'remote')")
    p_jobs.add_argument("--remote", action="store_true", help="only remote jobs")
    p_jobs.add_argument("--source", choices=ALL_SOURCES)
    p_jobs.add_argument("--company", help="substring match on company name")
    p_jobs.add_argument("--gated", action="store_true",
                        help="only jobs flagged as gated (account/login/captcha)")
    p_jobs.add_argument("--no-gated", action="store_true",
                        help="exclude gated jobs (apply-ready pile)")
    p_jobs.add_argument("--format", choices=["table", "json"], default="table")
    p_jobs.add_argument("--limit", type=int, default=50)
    p_jobs.set_defaults(func=cmd_jobs)

    p_enr = sub.add_parser("enrich", help="fetch full JD text for jobs missing descriptions")
    p_enr.add_argument("--limit", type=int, default=0,
                       help="max jobs to process (0 = no limit)")
    p_enr.add_argument("--source", choices=ALL_SOURCES,
                       help="only enrich jobs from this source")
    p_enr.add_argument("--retry-failed", action="store_true",
                       help="re-try jobs previously marked fetch_tier=failed")
    p_enr.add_argument("--dry-run", action="store_true",
                       help="print counts and exit without fetching")
    p_enr.add_argument("--save-every", type=int, default=25,
                       help="progress-print cadence (default 25)")
    p_enr.add_argument("--workers", type=int, default=8,
                       help="parallel network workers (default 8)")
    p_enr.add_argument("--batch-size", type=int, default=5,
                       help="jobs per Kimi extraction request (default 5)")
    p_enr.add_argument("--classify-backfill", action="store_true",
                       help="extract facts for jobs that already have descriptions "
                            "but no current extraction (no fetching)")
    p_enr.add_argument("--no-browser", action="store_true",
                       help="do not use local Playwright after HTTP retrieval fails")
    p_enr.add_argument("--no-extract", action="store_true",
                       help="retrieve descriptions without calling Kimi")
    p_enr.set_defaults(func=cmd_enrich)

    p_eval = sub.add_parser(
        "evaluate",
        help="evaluate enriched job facts against the local profile",
    )
    p_eval.add_argument("--profile", default="profile.json")
    p_eval.add_argument("--limit", type=int, default=0)
    p_eval.add_argument("--show-reasons", action="store_true")
    p_eval.set_defaults(func=cmd_evaluate)

    p_dedupe = sub.add_parser(
        "dedupe",
        help="group cross-source duplicates without network requests",
    )
    p_dedupe.set_defaults(func=cmd_dedupe)

    p_live = sub.add_parser(
        "verify-live",
        help="batch-check top ranked jobs through free ATS APIs",
    )
    p_live.add_argument("--top", type=int, default=20)
    p_live.add_argument("--workers", type=int, default=8)
    p_live.set_defaults(func=cmd_verify_live)

    p_match = sub.add_parser(
        "match",
        help="rank eligible jobs with local embeddings and deterministic features",
    )
    p_match.add_argument("--profile", default="profile.json")
    p_match.add_argument("--resume", default="resume.json")
    p_match.add_argument("--model", default="BAAI/bge-small-en-v1.5")
    p_match.add_argument("--top", type=int, default=50)
    p_match.add_argument("--minimum-score", type=float, default=0.0)
    p_match.add_argument("--format", choices=["table", "json"], default="table")
    p_match.add_argument("--rebuild-embeddings", action="store_true")
    p_match.set_defaults(func=cmd_match)

    p_tail = sub.add_parser(
        "tailor",
        help="create a structured Kimi-tailored resume for one eligible job",
    )
    p_tail.add_argument("job_id", help="job id (from `applyd jobs`)")
    p_tail.add_argument("--resume", default="resume.json")
    p_tail.add_argument("--template", default="resume_template.tex")
    p_tail.add_argument("--output", default="out")
    p_tail.add_argument("--model", default="moonshotai/kimi-k2.6")
    p_tail.add_argument("--force", action="store_true")
    p_tail.add_argument("--no-compile", action="store_true")
    p_tail.set_defaults(func=cmd_tailor)

    p_apply = sub.add_parser(
        "apply", help="fill one tailored application with Kimi and local Chrome"
    )
    p_apply.add_argument("job_id", help="job id already processed by `applyd tailor`")
    p_apply.add_argument("--model", default="moonshotai/kimi-k2.6")
    p_apply.add_argument(
        "--test-mode", type=_parse_bool, default=True,
        help="true fills without submitting; false permits a real submit (default true)",
    )
    p_apply.add_argument(
        "--browser", choices=["auto", "local", "brightdata"], default="auto",
        help="auto uses Bright Data for Lever and local Chrome otherwise",
    )
    p_apply.add_argument(
        "--liveness-ttl-minutes", type=int, default=15,
        help="reuse a recent ATS liveness result for this many minutes (default 15)",
    )
    p_apply.add_argument(
        "--max-cost-usd", type=float, default=None,
        help="optional hard cost stop (default disabled; 0 disables)",
    )
    p_apply.set_defaults(func=cmd_apply)

    p_batch = sub.add_parser(
        "apply-batch",
        help="serially tailor and apply to ranked jobs with CAPTCHA fallback",
    )
    p_batch.add_argument("--top", type=int, default=20)
    p_batch.add_argument("--minimum-score", type=float, default=70.0)
    p_batch.add_argument("--max-per-ats", type=int, default=6)
    p_batch.add_argument("--ats-failure-limit", type=int, default=3)
    p_batch.add_argument("--resume", default="resume.json")
    p_batch.add_argument("--template", default="resume_template.tex")
    p_batch.add_argument("--output", default="out")
    p_batch.add_argument("--tailor-model", default="moonshotai/kimi-k2.6")
    p_batch.add_argument("--apply-model", default="moonshotai/kimi-k2.6")
    p_batch.add_argument(
        "--test-mode", type=_parse_bool, default=True,
        help="true never submits; false permits real submissions (default true)",
    )
    p_batch.add_argument(
        "--captcha-fallback", choices=["none", "brightdata"], default="brightdata",
    )
    p_batch.add_argument("--max-cost-usd", type=float, default=0.0)
    p_batch.add_argument(
        "--max-total-apply-cost-usd", type=float, default=0.0,
        help="optional batch-dollar stop (default disabled; 0 disables)",
    )
    p_batch.add_argument("--report")
    p_batch.set_defaults(func=cmd_apply_batch)

    p_trace = sub.add_parser(
        "trace", help="show a redacted timeline for application attempts"
    )
    p_trace.add_argument("job_id")
    p_trace.add_argument("--all", action="store_true", help="show every attempt")
    p_trace.add_argument(
        "--errors-only", action="store_true", help="show only failed actions and verdicts"
    )
    p_trace.add_argument("--compare", action="store_true", help="compare attempt summaries")
    p_trace.add_argument("--format", choices=["text", "json"], default="text")
    p_trace.set_defaults(func=cmd_trace)

    p_gaps = sub.add_parser(
        "profile-gaps", help="list required application questions missing from the profile"
    )
    p_gaps.add_argument("--all", action="store_true")
    p_gaps.add_argument("--format", choices=["table", "json"], default="table")
    p_gaps.set_defaults(func=cmd_profile_gaps)

    p_res = sub.add_parser("resolve", help="debug: resolve a company name to (ATS, slug)")
    p_res.add_argument("company", help="company name to resolve, e.g. 'Stripe'")
    p_res.add_argument("--search-provider", choices=["brave", "serper"])
    p_res.set_defaults(func=cmd_resolve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
