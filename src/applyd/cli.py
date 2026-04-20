from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import httpx

from .config import load_env
from .discovery import ATS_MODULES, ResolverCache, aggregators, resolve
from .discovery.cache import BroadSearchCache
from .discovery.routing import detect_ats, extract_company_slug
from .discovery.search import BraveSearch, SearchProvider, SerperSearch
from .enrichment import MIN_USEFUL_CHARS, SpiderClient, fetch_text
from .filters import filter_jobs
from .models import Job
from .store import JobStore
from .tailor import (
    DEFAULT_MODEL as TAILOR_DEFAULT_MODEL,
    TailorClient,
    compile_pdf,
    tectonic_available,
    validate as validate_tailored,
)


SOURCES_AGGREGATORS = ["simplifyjobs", "broad_search"]
SOURCES_ATS = list(ATS_MODULES.keys())
ALL_SOURCES = SOURCES_AGGREGATORS + SOURCES_ATS


def _make_provider(name: str, client: httpx.Client) -> SearchProvider:
    name = name.lower()
    if name == "brave":
        return BraveSearch(client=client)
    if name == "serper":
        return SerperSearch(client=client)
    raise RuntimeError(f"unknown SEARCH_PROVIDER: {name}")


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
                provider = _make_provider(provider_name, client)
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


def cmd_jobs(args: argparse.Namespace) -> int:
    store = JobStore(Path(args.store))
    store.load()

    remote_filter: Optional[bool] = True if args.remote else None

    jobs = filter_jobs(
        store.all(),
        level=args.level,
        specialty=args.specialty,
        location=args.location,
        remote=remote_filter,
        source=args.source,
        company=args.company,
    )

    jobs.sort(
        key=lambda j: (j.posted_at is None, j.posted_at or 0),
        reverse=True,
    )

    if args.format == "json":
        print(json.dumps(
            [j.model_dump(mode="json") for j in jobs[: args.limit]],
            indent=2, default=str,
        ))
    else:
        shown = jobs[: args.limit]
        for j in shown:
            loc = ", ".join(j.locations) if j.locations else "—"
            print(
                f"[{j.source:16s}] "
                f"{j.company[:28]:28s} | "
                f"{j.title[:48]:48s} | "
                f"{loc[:28]:28s} | "
                f"{j.url}"
            )
        print(
            f"\n{len(jobs)} matching (showing {len(shown)})",
            file=sys.stderr,
        )
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    """Fetch full descriptions for jobs that lack them.
    Cascade: plain httpx+trafilatura → spider.cloud smart → spider.cloud chrome."""
    load_env()
    store = JobStore(Path(args.store))
    store.load()

    def needs_enrichment(j: Job) -> bool:
        if not j.url:
            return False
        if j.description and len(j.description) >= MIN_USEFUL_CHARS:
            return False
        if j.fetch_tier == "failed" and not args.retry_failed:
            return False
        return True

    candidates = [j for j in store.all() if needs_enrichment(j)]

    if args.source:
        candidates = [j for j in candidates if j.source == args.source]

    if args.limit:
        candidates = candidates[: args.limit]

    print(
        f"→ {len(candidates)} jobs to enrich "
        f"(store has {len(store.all())} total)",
        file=sys.stderr,
    )
    if args.dry_run:
        by_source: dict[str, int] = {}
        for j in candidates:
            by_source[j.source] = by_source.get(j.source, 0) + 1
        for src, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
            print(f"  {src:16s} {n}", file=sys.stderr)
        return 0

    spider: Optional[SpiderClient] = None
    try:
        spider = SpiderClient()
        print("  (spider.cloud available for tier 3)", file=sys.stderr)
    except RuntimeError as e:
        print(f"  (tier 3 disabled: {e})", file=sys.stderr)

    stats = {"ats": 0, "http": 0, "spider": 0, "spider-chrome": 0, "failed": 0}
    save_every = max(1, args.save_every)
    board_cache: dict = {}  # (ats, company) -> list[Job]; tier-1 session cache
    workers = max(1, args.workers)

    limits = httpx.Limits(
        max_connections=workers * 3, max_keepalive_connections=workers * 2,
    )
    with httpx.Client(
        timeout=60.0,
        headers={"User-Agent": "applyd/0.1"},
        limits=limits,
    ) as client:
        if spider is not None:
            spider._client = client

        def work(job: Job) -> tuple[Job, str, str, Optional[str]]:
            try:
                text, tier, err = fetch_text(
                    job.url, spider=spider, client=client, board_cache=board_cache,
                )
            except Exception as e:
                text, tier, err = "", "failed", f"{type(e).__name__}: {e}"
            return job, text, tier, err

        from concurrent.futures import ThreadPoolExecutor, as_completed

        processed = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, j) for j in candidates]
            try:
                for fut in as_completed(futures):
                    job, text, tier, err = fut.result()
                    if text:
                        job.description = text
                    job.description_fetched_at = datetime.now(timezone.utc)
                    job.fetch_tier = tier
                    job.fetch_error = err
                    stats[tier] = stats.get(tier, 0) + 1

                    processed += 1
                    if processed % save_every == 0:
                        store.save()
                        running = " ".join(f"{k}={v}" for k, v in stats.items())
                        print(
                            f"  {processed}/{len(candidates)}  {running}",
                            file=sys.stderr,
                        )
            except KeyboardInterrupt:
                print("\n  interrupted; saving partial progress...", file=sys.stderr)
                for f in futures:
                    f.cancel()
                raise

    store.save()
    running = " ".join(f"{k}={v}" for k, v in stats.items())
    print(f"\n✓ enrichment complete: {running}", file=sys.stderr)
    return 0


def _slugify(text: str, maxlen: int = 60) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in text).lower()
    slug = "-".join(p for p in slug.split("-") if p)
    return slug[:maxlen] or "untitled"


def _strip_fences(text: str) -> str:
    """Strip leading/trailing markdown code fences if the model adds them."""
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
    if text.endswith("```"):
        text = text[: text.rfind("```")].rstrip()
    return text.strip()


def cmd_tailor(args: argparse.Namespace) -> int:
    load_env()
    store = JobStore(Path(args.store))
    store.load()

    job = next((j for j in store.all() if j.id == args.job_id), None)
    if job is None:
        print(f"✗ job not found: {args.job_id}", file=sys.stderr)
        return 1

    if not job.description or len(job.description) < 200:
        print(
            f"✗ job {args.job_id} has no usable description. "
            "Run `applyd enrich` first or pick a different job.",
            file=sys.stderr,
        )
        return 1

    base_path = Path(args.base)
    if not base_path.exists():
        print(f"✗ base resume not found: {base_path}", file=sys.stderr)
        return 1
    base_tex = base_path.read_text(encoding="utf-8")

    slug = _slugify(f"{job.company}-{job.title}")
    outdir = Path("out") / slug
    outdir.mkdir(parents=True, exist_ok=True)

    print(
        f"→ tailoring [{job.company}] {job.title} (model: {args.model})...",
        file=sys.stderr,
    )
    try:
        client = TailorClient(model=args.model)
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1

    try:
        tailored, metadata, usage = client.tailor(
            base_resume_tex=base_tex,
            jd_text=job.description,
            company=job.company,
            role=job.title,
        )
    except Exception as e:
        print(f"✗ tailor call failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    tailored = _strip_fences(tailored)

    tex_path = outdir / "resume.tex"
    tex_path.write_text(tailored, encoding="utf-8")
    meta_path = outdir / "metadata.json"
    meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(f"  → wrote {tex_path} ({len(tailored)} chars)", file=sys.stderr)

    cache_c = usage.get("cache_creation_input_tokens", 0)
    cache_r = usage.get("cache_read_input_tokens", 0)
    print(
        f"  tokens: input={usage['input_tokens']} "
        f"output={usage['output_tokens']} "
        f"cache_write={cache_c} cache_read={cache_r}",
        file=sys.stderr,
    )

    # surface metadata — what the model decided
    if metadata:
        if metadata.get("parse_error"):
            print(f"  ⚠ metadata parse error: {metadata['parse_error']}", file=sys.stderr)
        if metadata.get("confidence"):
            print(f"  confidence: {metadata['confidence']}", file=sys.stderr)
        covered = metadata.get("keywords_covered") or []
        missing = metadata.get("keywords_missing") or []
        if covered or missing:
            print(f"  keywords covered ({len(covered)}): {', '.join(covered)}", file=sys.stderr)
            print(f"  keywords missing ({len(missing)}): {', '.join(missing)}", file=sys.stderr)
        for d in metadata.get("decisions_log") or []:
            print(f"  • {d}", file=sys.stderr)
        for r in metadata.get("risk_flags") or []:
            print(f"  ⚠ risk: {r}", file=sys.stderr)

    result = validate_tailored(base_tex, tailored)
    for w in result.warnings:
        print(f"  ⚠ {w}", file=sys.stderr)
    if not result.ok:
        for e in result.errors:
            print(f"  ✗ {e}", file=sys.stderr)
        if not args.ignore_errors:
            print(
                "  skipping PDF compile due to validation errors. "
                "Use --ignore-errors to force.",
                file=sys.stderr,
            )
            return 3

    if args.no_compile:
        print(f"\n✓ tailored .tex ready: {tex_path}", file=sys.stderr)
        return 0

    if not tectonic_available():
        print(
            "  ⚠ tectonic not found on PATH; skipping PDF compile. "
            "Install: brew install tectonic",
            file=sys.stderr,
        )
        return 0

    print("→ compiling PDF with tectonic...", file=sys.stderr)
    try:
        pdf = compile_pdf(tex_path, outdir=outdir)
        print(f"  → wrote {pdf}", file=sys.stderr)
    except RuntimeError as e:
        print(f"  ✗ compile failed: {e}", file=sys.stderr)
        return 4

    print(f"\n✓ tailor complete: {tex_path} + {pdf}", file=sys.stderr)
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    """Debug: resolve a single company name via the configured search provider."""
    load_env()
    provider_name = args.search_provider or os.environ.get("SEARCH_PROVIDER", "brave")
    with httpx.Client(timeout=30.0) as client:
        try:
            provider = _make_provider(provider_name, client)
        except Exception as e:
            print(f"can't build provider '{provider_name}': {e}", file=sys.stderr)
            return 1
        result = resolve(args.company, provider)
    if result is None:
        print(f"no confident match for '{args.company}'", file=sys.stderr)
        return 2
    print(f"{args.company} → {result[0]}:{result[1]}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="applyd",
        description="Autonomous job application engine — discovery layer",
    )
    parser.add_argument(
        "--store",
        default="data/jobs.json",
        help="path to jobs.json store (default: data/jobs.json)",
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

    p_jobs = sub.add_parser("jobs", help="query jobs from the store")
    p_jobs.add_argument("--level", choices=["new_grad", "mid", "senior"])
    p_jobs.add_argument("--specialty",
                        help="ml | backend | frontend | fullstack | mobile | infra | data | security")
    p_jobs.add_argument("--location", help="substring match (e.g. 'new york', 'remote')")
    p_jobs.add_argument("--remote", action="store_true", help="only remote jobs")
    p_jobs.add_argument("--source", choices=ALL_SOURCES)
    p_jobs.add_argument("--company", help="substring match on company name")
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
                       help="incremental save cadence (default 25)")
    p_enr.add_argument("--workers", type=int, default=8,
                       help="parallel fetch workers (default 8)")
    p_enr.set_defaults(func=cmd_enrich)

    p_tail = sub.add_parser("tailor", help="generate a tailored resume for a job")
    p_tail.add_argument("job_id", help="job id (from `applyd jobs`)")
    p_tail.add_argument("--base", default="resume_base.tex",
                        help="path to base resume .tex (default: resume_base.tex)")
    p_tail.add_argument("--model", default=TAILOR_DEFAULT_MODEL,
                        help=f"Claude model (default: {TAILOR_DEFAULT_MODEL})")
    p_tail.add_argument("--no-compile", action="store_true",
                        help="skip tectonic PDF compile, just write .tex")
    p_tail.add_argument("--ignore-errors", action="store_true",
                        help="compile PDF even if validator reports errors")
    p_tail.set_defaults(func=cmd_tailor)

    p_res = sub.add_parser("resolve", help="debug: resolve a company name to (ATS, slug)")
    p_res.add_argument("company", help="company name to resolve, e.g. 'Stripe'")
    p_res.add_argument("--search-provider", choices=["brave", "serper"])
    p_res.set_defaults(func=cmd_resolve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
