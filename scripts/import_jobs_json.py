"""One-time import: data/jobs.json (legacy single-tenant store) → Supabase.

Imports shared-catalog fields only — jobs + companies. Per-user fields on
the legacy Job (resume_pdf_path, apply_status, apply_attempted_at, apply_note)
are intentionally dropped and remain in data/jobs.json for a future backfill
into public.applications once auth is wired and the original user has an id.

Companies are resolved through CompaniesRepo's cascade (alias → ATS slug →
domain → normalized name → insert), but cached in-process so each unique
company name only round-trips Supabase once.

Usage:
    python scripts/import_jobs_json.py [--dry-run] [--limit N] [--batch 500] [--store PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from applyd.config import load_env
from applyd.db import CompaniesRepo, get_client
from applyd.discovery.routing import detect_gate
from applyd.models import Job


def main() -> int:
    p = argparse.ArgumentParser(description="Import data/jobs.json into Supabase")
    p.add_argument("--store", default="data/jobs.json")
    p.add_argument("--dry-run", action="store_true",
                   help="parse + count but write nothing")
    p.add_argument("--limit", type=int, default=0,
                   help="cap rows imported (0 = no cap)")
    p.add_argument("--batch", type=int, default=500,
                   help="rows per upsert batch (default 500)")
    args = p.parse_args()

    load_env()
    sb = get_client()
    crepo = CompaniesRepo(sb)

    store_path = Path(args.store)
    if not store_path.exists():
        print(f"✗ store not found: {store_path}", file=sys.stderr)
        return 1
    print(f"→ loading {store_path}...")
    raw = json.loads(store_path.read_text())
    print(f"  {len(raw)} jobs in source")

    # Parse + count per-user state being dropped
    jobs: list[Job] = []
    dropped = {"resume_pdf_path": 0, "apply_status": 0,
               "apply_attempted_at": 0, "apply_note": 0}
    parse_errors = 0
    for jid, jdict in raw.items():
        for k in dropped:
            if jdict.get(k):
                dropped[k] += 1
        try:
            jobs.append(Job.model_validate(jdict))
        except Exception as e:
            parse_errors += 1
            if parse_errors <= 3:
                print(f"  parse error on {jid}: {type(e).__name__}: {e}",
                      file=sys.stderr)
        if args.limit and len(jobs) >= args.limit:
            break

    print(f"  parsed: {len(jobs)} jobs ({parse_errors} parse errors)")
    print(f"  per-user state dropped: {dropped}")

    # Resolve companies first, cache in-process
    unique_names = sorted({j.company for j in jobs if j.company})
    print(f"\n→ resolving {len(unique_names)} unique companies...")
    company_id_by_name: dict[str, str] = {}
    if not args.dry_run:
        t0 = time.time()
        for i, name in enumerate(unique_names, 1):
            try:
                company_id_by_name[name] = crepo.upsert(name)
            except Exception as e:
                print(f"  company error {name!r}: {type(e).__name__}: {e}",
                      file=sys.stderr)
            if i % 200 == 0 or i == len(unique_names):
                print(f"  {i}/{len(unique_names)} ({time.time()-t0:.1f}s)")
        print(f"  ✓ resolved {len(company_id_by_name)} companies in "
              f"{time.time()-t0:.1f}s")

    # Build job rows mirroring _job_to_row in jobs_repo
    print("\n→ building rows...")
    rows: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for j in jobs:
        gate = j.apply_gate or (detect_gate(j.url) if j.url else None)
        rows.append({
            "id": j.id,
            "company_id": company_id_by_name.get(j.company),
            "ats": j.source,
            "ats_job_id": j.external_id,
            "url": j.url,
            "apply_gate": gate,
            "title": j.title,
            "description": j.description,
            "locations": j.locations,
            "is_remote": j.remote,
            "posted_at": j.posted_at.isoformat() if j.posted_at else None,
            "discovered_at": (j.first_seen_at.isoformat()
                              if j.first_seen_at else now_iso),
            "enriched_at": (j.description_fetched_at.isoformat()
                            if j.description_fetched_at else None),
            "last_seen_at": (j.last_seen_at.isoformat()
                             if j.last_seen_at else now_iso),
            "active": j.active,
            "fetch_tier": j.fetch_tier,
            "fetch_error": j.fetch_error,
            "raw_payload": j.raw,
        })
    print(f"  {len(rows)} rows ready")

    if args.dry_run:
        print("\n(dry run — no writes)")
        return 0

    # Batched upsert
    print(f"\n→ upserting in batches of {args.batch}...")
    t0 = time.time()
    written = 0
    for i in range(0, len(rows), args.batch):
        chunk = rows[i:i + args.batch]
        try:
            sb.table("jobs").upsert(chunk, on_conflict="id").execute()
            written += len(chunk)
        except Exception as e:
            print(f"  ✗ batch {i // args.batch} failed: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
        if (i // args.batch) % 4 == 0:
            print(f"  {written}/{len(rows)} ({time.time()-t0:.1f}s)")
    print(f"  ✓ {written}/{len(rows)} jobs upserted in "
          f"{time.time()-t0:.1f}s")

    # Verify counts
    total = sb.table("jobs").select("*", count="exact").limit(0).execute().count
    active = (sb.table("jobs").select("*", count="exact")
              .eq("active", True).limit(0).execute().count)
    co = sb.table("companies").select("*", count="exact").limit(0).execute().count
    print(f"\n→ final state: jobs={total} (active={active})  companies={co}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
