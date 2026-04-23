from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable, Optional

import httpx

from ..config import load_env
from ..discovery import ATS_MODULES, ResolverCache, aggregators, resolve
from ..discovery.cache import BroadSearchCache
from ..discovery.routing import detect_ats, extract_company_slug
from ..discovery.search import SearchProvider, make_provider
from ..models import Job
from ..store import JobStore


def _seed_cache_from_jobs(cache: ResolverCache, jobs: Iterable[Job]) -> int:
    """Extract (company, ats, slug) from ATS-hosted aggregator URLs and seed cache.
    Never overwrites an existing entry (brave resolutions win over aggregator guesses)."""
    seeded = 0
    seen: set[str] = set()
    for job in jobs:
        if not job.company or not job.url:
            continue
        key = job.company.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        ats = detect_ats(job.url)
        if not ats:
            continue
        slug = extract_company_slug(job.url)
        if not slug:
            continue
        if cache.get(job.company):
            continue
        cache.set(job.company, ats, slug, source="aggregator")
        seeded += 1
    return seeded


def cmd_discover(args: argparse.Namespace) -> int:
    load_env()
    store = JobStore(Path(args.store))
    store.load()

    cache = ResolverCache(Path(args.cache))
    cache.load()

    errors: list[str] = []
    total_new = 0
    total_updated = 0

    # figure out what the user is asking for from targets.json
    targets_path = Path(args.targets)
    companies: list[str] = []
    broad_dorks: Optional[list[str]] = None  # None = use defaults, [] = disabled
    if targets_path.exists():
        try:
            targets = json.loads(targets_path.read_text())
            companies = [c for c in targets.get("companies", []) if c]
            if "broad_dorks" in targets:
                broad_dorks = [d for d in targets["broad_dorks"] if d]
        except Exception as e:
            print(f"  ✗ failed to parse {targets_path}: {e}", file=sys.stderr)

    # skip broad search entirely if --no-broad or broad_dorks == []
    run_broad = not args.no_broad and broad_dorks != []
    need_provider = bool(companies) or run_broad

    with httpx.Client(timeout=60.0, headers={"User-Agent": "applyd/0.1"}) as client:
        provider: Optional[SearchProvider] = None
        if need_provider:
            provider_name = args.search_provider or os.environ.get(
                "SEARCH_PROVIDER", "brave"
            )
            try:
                provider = make_provider(provider_name, client)
            except Exception as e:
                print(
                    f"  ✗ can't build search provider '{provider_name}': {e}",
                    file=sys.stderr,
                )
                provider = None
        else:
            provider_name = "none"

        # 1. aggregator: SimplifyJobs (always runs, no user input, no credits)
        print("→ aggregator: SimplifyJobs...", file=sys.stderr)
        try:
            jobs = aggregators.simplifyjobs.fetch(client)
            new, updated = store.upsert(jobs)
            total_new += new
            total_updated += updated
            seeded = _seed_cache_from_jobs(cache, jobs)
            print(
                f"  {len(jobs)} postings, +{new} new, {updated} updated, "
                f"{seeded} cache seeds",
                file=sys.stderr,
            )
        except Exception as e:
            msg = f"simplifyjobs: {e}"
            errors.append(msg)
            print(f"  ✗ {msg}", file=sys.stderr)

        # 2. aggregator: broad search (Brave dorks → discovered ATS companies)
        if run_broad:
            ttl_hours = float(os.environ.get("BROAD_SEARCH_TTL_HOURS", "6"))
            broad_cache = BroadSearchCache(Path(args.broad_cache), ttl_hours=ttl_hours)
            broad_cache.load()
            print(f"\n→ aggregator: broad_search (provider={provider_name}, "
                  f"ttl={ttl_hours}h)...", file=sys.stderr)
            try:
                jobs, stats = aggregators.broad_search.discover(
                    provider=provider,
                    keyword_queries=broad_dorks,
                    client=client,
                    cache=broad_cache,
                )
                new, updated = store.upsert(jobs)
                total_new += new
                total_updated += updated
                per_ats = ", ".join(
                    f"{k}={v}" for k, v in sorted(stats["per_ats"].items())
                ) or "—"
                print(
                    f"  {stats['queries_total']} dorks: "
                    f"{stats['cache_hits']} cache hit, "
                    f"{stats['queries_run']} fresh queries"
                    + (f", {stats['skipped_no_provider']} skipped (no provider)"
                       if stats["skipped_no_provider"] else "")
                    + f" → {stats['discovered_companies']} companies → "
                    f"{len(jobs)} postings ({per_ats}), "
                    f"+{new} new, {updated} updated",
                    file=sys.stderr,
                )
                if stats["search_errors"] or stats["fetch_errors"]:
                    print(
                        f"    ({stats['search_errors']} search errors, "
                        f"{stats['fetch_errors']} fetch errors)",
                        file=sys.stderr,
                    )
                broad_cache.save()
            except Exception as e:
                msg = f"broad_search: {e}"
                errors.append(msg)
                print(f"  ✗ {msg}", file=sys.stderr)

        # 3. user-specified companies (targets.json → resolver → ATS)
        if companies:
            print(
                f"\n→ {len(companies)} user-specified companies "
                f"(resolver: {provider_name})",
                file=sys.stderr,
            )
            for company in companies:
                resolved = cache.get(company)
                if not resolved:
                    if provider is None:
                        print(f"  ? '{company}': no cache + no provider, skipping",
                              file=sys.stderr)
                        continue
                    print(f"→ resolving '{company}'...", file=sys.stderr)
                    try:
                        resolved = resolve(company, provider)
                    except Exception as e:
                        msg = f"resolver '{company}': {e}"
                        errors.append(msg)
                        print(f"  ✗ {msg}", file=sys.stderr)
                        resolved = None
                    if resolved:
                        cache.set(
                            company, resolved[0], resolved[1],
                            source=provider_name,
                        )
                        print(f"  → {resolved[0]}:{resolved[1]}", file=sys.stderr)
                    else:
                        print(f"  ✗ no confident match for '{company}'",
                              file=sys.stderr)
                        continue

                ats, slug = resolved
                module = ATS_MODULES.get(ats)
                if module is None:
                    print(f"  ? unknown ATS '{ats}' for '{company}'", file=sys.stderr)
                    continue
                print(f"→ {ats}:{slug} ({company})...", file=sys.stderr)
                try:
                    jobs = module.fetch(slug, client)
                    new, updated = store.upsert(jobs)
                    total_new += new
                    total_updated += updated
                    print(
                        f"  {len(jobs)} postings, +{new} new, {updated} updated",
                        file=sys.stderr,
                    )
                except Exception as e:
                    msg = f"{ats}:{slug}: {e}"
                    errors.append(msg)
                    print(f"  ✗ {msg}", file=sys.stderr)

    cache.save()
    store.save()
    total = len(store.all())
    print(
        f"\n✓ discover complete: +{total_new} new, {total_updated} updated, "
        f"{total} total in store",
        file=sys.stderr,
    )
    if errors:
        print(f"  {len(errors)} error(s) above", file=sys.stderr)
    return 0
