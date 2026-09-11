from __future__ import annotations

import argparse
import os
from pathlib import Path

from ..apply.browser import persistent_local_page
from ..config import load_env


def cmd_browser_login(args: argparse.Namespace) -> int:
    """Open applyd's dedicated persistent Chrome profile for manual setup."""
    load_env()
    profile = Path(
        args.profile
        or os.environ.get("APPLYD_BROWSER_PROFILE", "data/browser/apply-profile")
    ).expanduser()
    profile.mkdir(parents=True, exist_ok=True)
    print(f"Opening dedicated applyd Chrome profile: {profile.resolve()}")
    print("Sign in only to sites you want applyd to access.")
    print("Do not use Chrome's everyday User Data directory for this command.")
    with persistent_local_page(profile_dir=profile, headless=False) as page:
        page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
        try:
            input("When browser setup is complete, return here and press Enter to save and close: ")
        except EOFError:
            print("No interactive terminal detected; closing the setup browser.")
    print(f"Saved browser state in {profile.resolve()}")
    return 0
