from __future__ import annotations

import argparse
import os
import sys

import httpx

from ..config import load_env
from ..discovery import resolve
from ..discovery.search import make_provider


def cmd_resolve(args: argparse.Namespace) -> int:
    """Debug: resolve a single company name via the configured search provider."""
    load_env()
    provider_name = args.search_provider or os.environ.get("SEARCH_PROVIDER", "brave")
    with httpx.Client(timeout=30.0) as client:
        try:
            provider = make_provider(provider_name, client)
        except Exception as e:
            print(f"can't build provider '{provider_name}': {e}", file=sys.stderr)
            return 1
        result = resolve(args.company, provider)
    if result is None:
        print(f"no confident match for '{args.company}'", file=sys.stderr)
        return 2
    print(f"{args.company} → {result[0]}:{result[1]}")
    return 0
