from __future__ import annotations

import argparse
import sys
from typing import Optional

from .commands import (
    cmd_discover,
    cmd_enrich,
    cmd_jobs,
    cmd_resolve,
    cmd_tailor,
)
from .discovery import ATS_MODULES


SOURCES_AGGREGATORS = ["simplifyjobs", "broad_search"]
SOURCES_ATS = list(ATS_MODULES.keys())
ALL_SOURCES = SOURCES_AGGREGATORS + SOURCES_ATS


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="applyd",
        description="Autonomous job application engine (Supabase-backed)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

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
                       help="parallel fetch workers (default 8)")
    p_enr.set_defaults(func=cmd_enrich)

    p_tail = sub.add_parser(
        "tailor",
        help="tailor a resume for one (user, job). Dev/admin shim over tailor_for_user.",
    )
    p_tail.add_argument("job_id", help="job id (from `applyd jobs`)")
    p_tail.add_argument(
        "--user",
        help="user UUID. Falls back to APPLYD_DEV_USER_ID if unset.",
    )
    p_tail.set_defaults(func=cmd_tailor)

    p_res = sub.add_parser("resolve", help="debug: resolve a company name to (ATS, slug)")
    p_res.add_argument("company", help="company name to resolve, e.g. 'Stripe'")
    p_res.add_argument("--search-provider", choices=["brave", "serper"])
    p_res.set_defaults(func=cmd_resolve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
