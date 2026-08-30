"""Local Playwright fallback for JavaScript-rendered job descriptions."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import trafilatura
from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from .fetcher import MIN_USEFUL_CHARS


class LocalBrowserRetriever:
    def __init__(self, context: BrowserContext) -> None:
        self.context = context

    def fetch(self, url: str) -> str | None:
        page: Page = self.context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                time.sleep(1)
            content = page.content()
            text = trafilatura.extract(
                content,
                output_format="markdown",
                include_tables=False,
                favor_precision=True,
            ) or ""
            return text if len(text) >= MIN_USEFUL_CHARS else None
        except Exception:
            return None
        finally:
            page.close()


@contextmanager
def local_browser_retriever(
    profile_dir: str | Path | None = None,
    *,
    headless: bool | None = None,
) -> Iterator[LocalBrowserRetriever]:
    """Open a dedicated persistent Chrome profile for retrieval fallbacks."""
    if profile_dir is None:
        profile_dir = os.environ.get(
            "APPLYD_RETRIEVAL_PROFILE", "data/browser/retrieval-profile"
        )
    if headless is None:
        headless = os.environ.get("APPLYD_BROWSER_HEADLESS", "true").lower() != "false"
    profile = Path(profile_dir)
    profile.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = _launch(playwright, profile, headless=headless)
        try:
            yield LocalBrowserRetriever(context)
        finally:
            context.close()


def _launch(playwright: Playwright, profile: Path, *, headless: bool) -> BrowserContext:
    args = ["--disable-blink-features=AutomationControlled"]
    try:
        return playwright.chromium.launch_persistent_context(
            str(profile),
            channel="chrome",
            headless=headless,
            args=args,
        )
    except Exception:
        return playwright.chromium.launch_persistent_context(
            str(profile),
            headless=headless,
            args=args,
        )
