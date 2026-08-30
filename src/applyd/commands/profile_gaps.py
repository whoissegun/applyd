from __future__ import annotations

import argparse
import json

from ..local_store import get_local_store


def cmd_profile_gaps(args: argparse.Namespace) -> int:
    rows = get_local_store().list_profile_question_gaps(include_closed=args.all)
    if args.format == "json":
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("No unresolved required-question gaps.")
        return 0
    print("Count  Category          Last company             Required question")
    print("-----  ----------------  -----------------------  ------------------------------")
    for row in rows:
        print(
            f"{int(row['occurrences']):>5}  {row['category'][:16]:16}  "
            f"{str(row.get('last_company') or '')[:23]:23}  {row['sample_label']}"
        )
    return 0
