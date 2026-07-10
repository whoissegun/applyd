from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright


BLOCK_RESOURCE_TYPES = {"image", "media", "font"}

# connect_over_cdp retry: Bright Data refuses a connect with
# "internal server error (browser_in_use)" while a previous session under the
# same auth string is still tearing down (an os._exit'd watchdog kill leaves
# the remote session alive until the ~5-min inactivity timeout — there is no
# API to kill it remotely). One in-process retry covers the fast case; past
# that we raise BrowserConnectError so the caller requeues + backs off instead
# of burning the application as 'failed' (one app burned 17 attempts on this,
# 2026-07-07).
CONNECT_ATTEMPTS = 2
CONNECT_RETRY_SLEEP_SECONDS = 20


class BrowserConnectError(Exception):
    """Could not obtain a Bright Data browser session (busy/refused/unreachable).
    Always raised BEFORE any LLM spend — safe to requeue and retry later."""


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
    user = f"brd-customer-{customer}-zone-{zone}"
    # Pin the proxy exit country (e.g. "ca"). A job application arriving from
    # a random overseas datacenter IP is a huge captcha risk signal — hCaptcha
    # served us a challenge in Polish before this was set.
    country = os.environ.get("BRIGHTDATA_COUNTRY", "").strip().lower()
    if country:
        user += f"-country-{country}"
    return f"wss://{user}:{password}@{host}:{port}"


@contextmanager
def brightdata_page(block_heavy: bool = True) -> Iterator[Page]:
    """Open a Bright Data Scraping Browser page.

    `block_heavy=True` aborts image/media/font requests to cut proxy bandwidth.
    Leaves HTML/CSS/JS/XHR alone since reCAPTCHA + form SDKs need them.
    """
    with sync_playwright() as p:
        url = brightdata_cdp_url()
        browser: Browser | None = None
        last_exc: Exception | None = None
        for attempt in range(CONNECT_ATTEMPTS):
            try:
                browser = p.chromium.connect_over_cdp(url)
                break
            except Exception as e:  # noqa: BLE001
                last_exc = e
                print(
                    f"✗ CDP connect attempt {attempt + 1}/{CONNECT_ATTEMPTS} failed: "
                    f"{type(e).__name__}: {str(e)[:200]}",
                    file=sys.stderr,
                )
                if attempt + 1 < CONNECT_ATTEMPTS:
                    time.sleep(CONNECT_RETRY_SLEEP_SECONDS)
        if browser is None:
            raise BrowserConnectError(
                f"connect_over_cdp failed after {CONNECT_ATTEMPTS} attempts: "
                f"{type(last_exc).__name__}: {str(last_exc)[:300]}"
            ) from last_exc
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
