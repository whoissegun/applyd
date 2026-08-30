from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright


# Bandwidth blocking for the remote browser, done BROWSER-SIDE via CDP
# Network.setBlockedURLs. Do NOT replace this with page.route(): client-side
# interception routes every request (hCaptcha assets, telemetry — dozens per
# form) through Python over the same websocket that carries click/evaluate,
# and that combination hangs intermittently over connect_over_cdp
# (microsoft/playwright#11776, ~30% of runs — matched our 2-of-3 hang rate on
# Palantir Lever, 2026-07-10). Extension patterns instead of resource types is
# the trade-off; unextensioned images slip through at ~$8/GB pocket change.
BLOCKED_URL_PATTERNS = [
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.avif", "*.svg", "*.ico",
    "*.woff", "*.woff2", "*.ttf", "*.otf", "*.eot",
    "*.mp4", "*.webm", "*.mp3", "*.ogg", "*.mov",
    "*google-analytics.com*", "*googletagmanager.com*", "*doubleclick.net*",
    "*facebook.net*", "*hotjar.com*", "*segment.io*",
]

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

    `block_heavy=True` blocks image/media/font URLs browser-side (CDP
    Network.setBlockedURLs) to cut proxy bandwidth. Leaves HTML/CSS/JS/XHR
    alone since reCAPTCHA + form SDKs need them.
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
                # Best-effort: blocking is a bandwidth optimization, never
                # worth failing the apply over.
                try:
                    cdp = context.new_cdp_session(page)
                    cdp.send("Network.enable")
                    cdp.send(
                        "Network.setBlockedURLs", {"urls": BLOCKED_URL_PATTERNS}
                    )
                except Exception as e:  # noqa: BLE001
                    print(
                        f"⚠ Network.setBlockedURLs failed ({type(e).__name__}: "
                        f"{str(e)[:120]}); continuing without resource blocking",
                        file=sys.stderr,
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


@contextmanager
def persistent_local_page(
    *,
    profile_dir: str | Path | None = None,
    headless: bool | None = None,
) -> Iterator[Page]:
    """Open the dedicated persistent Chrome profile used for real local applies."""
    if profile_dir is None:
        profile_dir = os.environ.get(
            "APPLYD_BROWSER_PROFILE", "data/browser/apply-profile"
        )
    if headless is None:
        headless = os.environ.get("APPLYD_BROWSER_HEADLESS", "true").lower() != "false"
    profile = Path(profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                str(profile),
                channel="chrome",
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            try:
                context = p.chromium.launch_persistent_context(
                    str(profile),
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except Exception as exc:
                raise BrowserConnectError(
                    f"could not launch local Chrome: {type(exc).__name__}: {exc}"
                ) from exc
        try:
            # A persistent profile may restore the tab from the previous run,
            # including its already-filled DOM. Reusing context.pages[0] made
            # a test-mode rerun appear to pass without exercising any fields.
            # Keep cookies/session state, but isolate every application in a
            # brand-new document.
            restored_pages = list(context.pages)
            page = context.new_page()
            for old_page in restored_pages:
                try:
                    old_page.close()
                except Exception:
                    pass
            yield page
        finally:
            context.close()


@contextmanager
def browser_page(
    provider: str | None = None, *, test_mode: bool = True
) -> Iterator[Page]:
    """Select the local default or optional Bright Data compatibility path.

    Test runs remain headless by default. Real local submissions use visible
    Chrome unless APPLYD_BROWSER_HEADLESS explicitly overrides it: Ashby
    rejected the same completed form as spam headless and accepted it headed.
    """
    provider = (provider or os.environ.get("APPLYD_BROWSER_PROVIDER", "local")).lower()
    if provider == "local":
        headless = None
        if not test_mode and "APPLYD_BROWSER_HEADLESS" not in os.environ:
            headless = False
        with persistent_local_page(headless=headless) as page:
            setattr(page, "_applyd_browser_provider", "local")
            yield page
        return
    if provider == "brightdata":
        with brightdata_page(block_heavy=True) as page:
            setattr(page, "_applyd_browser_provider", "brightdata")
            yield page
        return
    raise BrowserConnectError(f"unknown browser provider: {provider!r}")
