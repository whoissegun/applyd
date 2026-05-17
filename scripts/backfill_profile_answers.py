"""One-shot backfill: concatenate the now-dead narrative columns into
`user_profiles.profile_answers` as labeled sections, so the data survives the
20260517200000_collapse_narrative_into_profile_answers.sql column drop.

Idempotent: re-running is a no-op as long as the columns still exist. After
the SQL migration drops them, this script can't run (and shouldn't need to).

Usage:
    .venv/bin/python scripts/backfill_profile_answers.py
"""

from __future__ import annotations

import os

from applyd.config import load_env

load_env()

from supabase import create_client  # noqa: E402


def build_profile_answers(row: dict) -> str:
    parts: list[str] = []
    if tr := (row.get("target_roles") or "").strip():
        parts.append(f"## Target roles\n{tr}")

    wa_bits: list[str] = []
    if was := (row.get("work_auth_summary") or "").strip():
        wa_bits.append(was)
    if countries := row.get("sponsorship_needed_countries") or []:
        wa_bits.append(f"Need sponsorship to work in: {', '.join(countries)}")
    if wa_bits:
        parts.append("## Work authorization\n" + "\n".join(wa_bits))

    extras: list[str] = []
    if levels := row.get("target_levels") or []:
        extras.append(f"Target levels: {', '.join(levels)}")
    if specs := row.get("target_specialties") or []:
        extras.append(f"Target specialties: {', '.join(specs)}")
    if locs := row.get("target_locations"):
        if locs and locs != {}:
            extras.append(f"Target locations: {locs}")
    if extras:
        parts.append("## Targets (legacy)\n" + "\n".join(extras))

    existing = (row.get("profile_answers") or "").strip()
    if existing:
        parts.append(f"## Anything else\n{existing}")

    return "\n\n".join(parts).strip()


def main() -> None:
    sb = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SECRET_KEY"],
    )
    res = sb.table("user_profiles").select(
        "id, target_roles, work_auth_summary, sponsorship_needed_countries, "
        "target_levels, target_specialties, target_locations, profile_answers"
    ).execute()

    for row in res.data:
        blob = build_profile_answers(row)
        if not blob:
            print(f"skip {row['id']} — nothing to migrate")
            continue
        print(f"writing {row['id']} ({len(blob)} chars)")
        sb.table("user_profiles").update({"profile_answers": blob}).eq(
            "id", row["id"]
        ).execute()


if __name__ == "__main__":
    main()
