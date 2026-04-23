from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright


BLOCK_RESOURCE_TYPES = {"image", "media", "font"}


def _required(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(
            f"{name} not set. Add it to .env (see profile.example or README)."
        )
    return v


def brightdata_cdp_url() -> str:
    customer = _required("BRIGHTDATA_CUSTOMER_ID")
    zone = _required("BRIGHTDATA_ZONE")
    password = _required("BRIGHTDATA_ZONE_PASSWORD")
    host = os.environ.get("BRIGHTDATA_HOST", "brd.superproxy.io")
    port = os.environ.get("BRIGHTDATA_CDP_PORT", "9222")
    return f"wss://brd-customer-{customer}-zone-{zone}:{password}@{host}:{port}"


@contextmanager
def brightdata_page(block_heavy: bool = True) -> Iterator[Page]:
    """Open a Bright Data Scraping Browser page.

    `block_heavy=True` aborts image/media/font requests to cut proxy bandwidth.
    Leaves HTML/CSS/JS/XHR alone since reCAPTCHA + form SDKs need them.
    """
    with sync_playwright() as p:
        browser: Browser = p.chromium.connect_over_cdp(brightdata_cdp_url())
        try:
            context: BrowserContext = (
                browser.contexts[0] if browser.contexts else browser.new_context()
            )
            page = context.new_page()
            if block_heavy:
                page.route(
                    "**/*",
                    lambda route: (
                        route.abort()
                        if route.request.resource_type in BLOCK_RESOURCE_TYPES
                        else route.continue_()
                    ),
                )
            yield page
        finally:
            browser.close()


@contextmanager
def local_page(headless: bool = False, slow_mo_ms: int = 250) -> Iterator[Page]:
    """Open a local Chromium page for visual debugging.

    No Bright Data, no proxy, no resource blocking — you see exactly what loads.
    Default `headless=False` so a real window pops up; `slow_mo_ms` adds a small
    delay per action so you can watch clicks/types happen.
    """
    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        try:
            context = browser.new_context()
            page = context.new_page()
            yield page
        finally:
            browser.close()
