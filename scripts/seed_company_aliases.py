"""Seed company_aliases with known rebrands so future discovery dedups them.

For each (loser_name, winner_name) pair:
  - Ensure winner exists (upsert).
  - If a separate loser row exists, MERGE loser → winner.
  - If not, just add alias loser_name → winner_id.

Re-run safely — idempotent. Aliases are upserts; merges short-circuit when
the loser is already gone.

Conservative list: only well-publicized rebrands where one name has been
officially retired (Facebook→Meta 2021, Twitter→X 2023, Square→Block 2021).
Sister brands and acquisitions that still post jobs under their own name
(GitHub, Figma, etc.) are deliberately NOT aliased here.

Usage:
    python scripts/seed_company_aliases.py
"""
from __future__ import annotations

import sys
from typing import Optional

from applyd.config import load_env
from applyd.db import CompaniesRepo, get_client
from applyd.db.companies_repo import _normalize_name


# (loser_name, winner_name). loser_name → winner_name.
# Order matters: every alias for the same winner gets handled in this list.
REBRANDS = [
    # Facebook → Meta (officially renamed Oct 2021)
    ("Facebook", "Meta"),
    ("Facebook Inc", "Meta"),
    ("Facebook Inc.", "Meta"),
    ("Meta Platforms", "Meta"),
    ("Meta Platforms Inc", "Meta"),
    # Twitter → X (renamed July 2023)
    ("Twitter", "X"),
    ("Twitter Inc", "X"),
    ("X Corp", "X"),
    # Square → Block (renamed Dec 2021)
    ("Square", "Block"),
    ("Square Inc", "Block"),
]


def find_by_normalized(sb, name: str) -> Optional[str]:
    """Return the company id with this normalized name, or None."""
    norm = _normalize_name(name)
    if not norm:
        return None
    res = (
        sb.table("companies").select("id")
        .eq("name_normalized", norm).limit(1).execute().data
    )
    return res[0]["id"] if res else None


def main() -> int:
    load_env()
    sb = get_client()
    repo = CompaniesRepo(sb)

    print(f"→ seeding {len(REBRANDS)} alias pair(s)\n")

    for loser_name, winner_name in REBRANDS:
        loser_norm = _normalize_name(loser_name)
        winner_norm = _normalize_name(winner_name)

        # Edge case: loser and winner normalize to the same string — alias is
        # already implicit via the normalize index, skip.
        if loser_norm == winner_norm:
            print(f"  ⊘ {loser_name!r:30} → {winner_name!r:15}  "
                  f"(same normalized form, skipped)")
            continue

        # Ensure winner row exists.
        winner_id = repo.upsert(winner_name)

        # Look for an existing separate loser row.
        loser_id = find_by_normalized(sb, loser_name)

        if loser_id and loser_id != winner_id:
            result = repo.merge(loser_id=loser_id, winner_id=winner_id)
            print(f"  ↪ {loser_name!r:30} → {winner_name!r:15}  "
                  f"MERGED ({result['jobs_reassigned']} jobs, "
                  f"{result['aliases_reassigned']} aliases reassigned)")
        else:
            # Just bind the alias.
            repo.add_alias(loser_name, winner_id)
            print(f"  + {loser_name!r:30} → {winner_name!r:15}  alias added")

    # Print final state
    total_aliases = (sb.table("company_aliases").select("*", count="exact")
                     .limit(0).execute().count)
    print(f"\n→ {total_aliases} aliases in DB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
