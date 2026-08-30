"""Playwright tool dispatchers (ref-based) for the direct-apply runner.

Refs are stable per-page IDs we mint by setting `data-applyd-ref="rX"` on
each interactive element when snapshot() is called. Tools accept refs and
locate via `[data-applyd-ref="rX"]`. This decouples the LLM's vocabulary
from CSS selectors and makes failures observable (a click on a ref that
no longer exists fails clean instead of silently mis-clicking).

Combobox/dropdown handling is split into two explicit steps:
  open_dropdown(ref) → returns option list with new refs (o0, o1, ...)
  pick_option(option_ref) → clicks the chosen option
This replaces the previous opaque select_combobox helper that failed
silently when its DOM-shape guesses didn't match.
"""
from __future__ import annotations

import os
import random
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PWTimeout

from ..tailor.structured import escape_latex


# ── human-behavior knobs ─────────────────────────────────────────────────────
# reCAPTCHA v3 / invisible hCaptcha score a session on mouse movement and typing
# cadence. Instant .fill() (zero keystrokes) + a Bright Data browser that never
# moves the pointer is a maximal bot signature — on Greenhouse that trips the
# email-verification wall (the app is held pending a code we can't read, so it
# never reaches the recruiter). These add cheap realism to lift the score.
# Disable with APPLYD_HUMANIZE=false for debugging / speed.
_HUMANIZE = os.environ.get("APPLYD_HUMANIZE", "true").lower() != "false"
# Per-field typing cadence (ms between keystrokes); randomized per field so the
# rhythm isn't a constant. Long values (rare — cover letters are skipped) type a
# realistic prefix then fill the rest, so one pathological field can't blow the
# 300s wall-clock budget.
_TYPE_DELAY_MS = (40, 110)
_TYPE_HUMAN_PREFIX = 40
_TYPE_INSTANT_OVER = 120
_THINK_BETWEEN_FIELDS = (0.25, 1.1)
_THINK_BEFORE_SUBMIT = (0.8, 2.5)


def _should_humanize(page: Page) -> bool:
    """Humanize local Chrome only; remote CDP turns each keystroke into costly
    network chatter and Bright Data provides its own CAPTCHA solver."""
    return _HUMANIZE and getattr(page, "_applyd_browser_provider", "local") != "brightdata"


def _ok(msg: str) -> str:
    return f"ok: {msg}"


def _err(msg: str) -> str:
    return f"error: {msg}"


def _jitter(lo_hi: tuple[float, float]) -> float:
    return random.uniform(lo_hi[0], lo_hi[1])


def _human_hover(page: Page, loc) -> None:
    """Move the pointer to the element along a short interpolated path. Pure
    behavioral signal for the captcha scorer; best-effort, never raises."""
    try:
        box = loc.bounding_box(timeout=2000)
    except Exception:
        box = None
    if not box:
        return
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    page.mouse.move(x, y, steps=random.randint(6, 18))
    time.sleep(_jitter((0.05, 0.22)))


def _human_type(loc, value: str) -> None:
    """Type like a person: focus with a real click, clear, then keystroke the
    value with a jittered per-field cadence."""
    loc.click(timeout=10000)
    loc.fill("")  # clearing isn't a keystroke tell; keeps long re-fills cheap
    delay = random.randint(*_TYPE_DELAY_MS)
    if len(value) > _TYPE_INSTANT_OVER:
        loc.press_sequentially(value[:_TYPE_HUMAN_PREFIX], delay=delay)
        loc.press_sequentially(value[_TYPE_HUMAN_PREFIX:], delay=4)
    else:
        loc.press_sequentially(value, delay=delay)


def _ref_locator(page: Page, ref: str):
    return page.locator(f'[data-applyd-ref="{ref}"]').first


# ── navigate ───────────────────────────────────────────────────────────────

def navigate(page: Page, url: str, wait_ms: int = 30000) -> str:
    current = page.url or ""
    if current.rstrip("/") == url.rstrip("/"):
        return _err(
            f"navigate refused: already on {current}. Re-navigating would "
            f"clear the form. Use snapshot to re-inspect current state instead."
        )
    try:
        page.goto(url, timeout=wait_ms, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=10000)
        return _ok(f"loaded {page.url} | title={page.title()!r}")
    except PWTimeout:
        return _ok(f"loaded with timeout {page.url} | title={page.title()!r}")
    except Exception as e:
        return _err(f"navigate: {type(e).__name__}: {e}")


# ── snapshot ───────────────────────────────────────────────────────────────

_SNAPSHOT_JS = r"""() => {
    // Reset old refs (both element refs r* and option refs o*).
    document.querySelectorAll('[data-applyd-ref]').forEach(el => el.removeAttribute('data-applyd-ref'));

    let counter = 0;
    const out = [];

    const isVisible = (el) => {
        if (el.type === 'file') return true;
        // Hidden transport fields (captcha tokens, ATS metadata, internal
        // IDs) are never legitimate agent controls. Surfacing them wastes
        // context and risks accidental mutation.
        if (el.type === 'hidden') return false;
        const r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return false;
        if (el.getAttribute('aria-hidden') === 'true') return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        return true;
    };

    const isInteractive = (el) => {
        const tag = el.tagName.toLowerCase();
        if (['input', 'textarea', 'select', 'button'].includes(tag)) return true;
        const role = el.getAttribute('role');
        if (role && ['combobox', 'button', 'checkbox', 'radio', 'textbox', 'switch', 'menuitem', 'tab', 'searchbox'].includes(role)) return true;
        if (tag === 'a' && el.hasAttribute('href')) return true;
        return false;
    };

    const isHoneypot = (el) => {
        const tag = el.tagName.toLowerCase();
        // tabIndex=-1 inputs are usually anti-spam fields
        if (el.tabIndex === -1 && tag === 'input' && el.type !== 'file' && el.type !== 'submit' && el.type !== 'button') return true;
        return false;
    };

    for (const el of document.querySelectorAll('input, textarea, select, button, a[href], [role]')) {
        // Cap the scan: content-heavy pages (stripe.com/jobs has thousands of
        // anchors) make this loop crawl, and the output is truncated at 8k
        // chars anyway. Form controls appear long before ref 300 in practice.
        if (counter >= 300) break;
        if (!isInteractive(el)) continue;
        if (!isVisible(el)) continue;
        if (el.disabled) continue;
        if (isHoneypot(el)) continue;

        const candidateTag = el.tagName.toLowerCase();
        if (candidateTag === 'button') {
            const buttonText = (el.innerText || '').trim().toLowerCase();
            if (/^upload file$/.test(buttonText)) continue;
            if (buttonText === 'replace' || el.getAttribute('title') === 'Delete file') continue;
            if (/^\+?\s*add education$/.test(buttonText)) continue;
            if (!buttonText && el.parentElement?.querySelector('input[role="combobox"]')) continue;
        }

        const ref = `r${counter++}`;
        el.setAttribute('data-applyd-ref', ref);

        const tag = el.tagName.toLowerCase();
        const role = el.getAttribute('role') || tag;
        const inputType = el.type || '';

        const labelEl = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
        const aria = el.getAttribute('aria-label') || '';
        const ariaby = el.getAttribute('aria-labelledby');
        let labeledBy = '';
        if (ariaby) {
            const labeler = document.getElementById(ariaby);
            if (labeler) labeledBy = labeler.innerText || '';
        }
        const wrappedLabel = el.closest('label');
        const option = (labelEl?.innerText || wrappedLabel?.innerText || '').trim();
        let questionEl = el.closest('fieldset')?.querySelector(':scope > .ashby-application-form-question-title') || null;
        if (!questionEl && (!(inputType === 'radio' || inputType === 'checkbox') || !option)) {
            let questionScope = el.parentElement;
            for (let depth = 0; depth < 5 && questionScope; depth++, questionScope = questionScope.parentElement) {
                questionEl = questionScope.querySelector(':scope > .ashby-application-form-question-title');
                if (questionEl) break;
            }
        }
        const question = (questionEl?.innerText || '').trim();
        const ph = el.getAttribute('placeholder') || '';
        const name = el.getAttribute('name') || '';
        let label = question || option || aria || labeledBy || ph || name || el.innerText || '';
        if ((inputType === 'radio' || inputType === 'checkbox') && question && option && question !== option) {
            label = `${question} — ${option}`;
        }
        let semanticOption = option;
        if (candidateTag === 'button' && question) {
            const buttonChoice = (el.innerText || el.value || '').trim();
            if (buttonChoice && buttonChoice !== question && buttonChoice.length <= 80) {
                semanticOption = buttonChoice;
                label = `${question} — ${buttonChoice}`;
            }
        }
        label = label.trim().slice(0, 180);
        el.setAttribute('data-applyd-label', label.slice(0, 300));
        if (question) el.setAttribute('data-applyd-question', question.slice(0, 300));
        if (semanticOption) el.setAttribute('data-applyd-option', semanticOption.slice(0, 120));

        // Required detection: native, ARIA, or label text containing "*" or "(required)"
        const labelText = label.toLowerCase();
        const required = !!(el.required) ||
                         el.getAttribute('aria-required') === 'true' ||
                         labelText.includes('*') ||
                         labelText.includes('(required)') ||
                         labelText.includes('required)') ||
                         (questionEl?.className || '').toString().toLowerCase().includes('required');

        // React comboboxes (Greenhouse country/school/degree, react-select)
        // keep the chosen value in component state, not el.value — so a picked
        // field reads back empty and the agent re-picks it forever. pick_option
        // stamps the choice on data-applyd-picked; surface it as the value so
        // the field reads as satisfied. Survives snapshot (only *-ref is reset).
        const picked = el.getAttribute('data-applyd-picked');
        const rawValue = (el.value !== undefined && el.value !== '')
            ? el.value : (el.innerText || '');
        const value = (picked || rawValue || '').toString().slice(0, 80);

        out.push({ ref, role, type: inputType, label, required, value });
    }
    return out;
}"""


def snapshot(page: Page) -> str:
    """Return a structured list of interactive elements with stable refs."""
    try:
        blocking_captcha_frames = [
            frame.url for frame in page.frames
            if any(marker in (frame.url or "").casefold() for marker in (
                "captcha-delivery.com/interstitial",
                "captcha-delivery.com/captcha",
                "recaptcha/api2/bframe",
            ))
        ]
        if blocking_captcha_frames:
            return (
                "GATE DETECTED: CAPTCHA verification is blocking the application "
                f"document in a child frame ({blocking_captcha_frames[0][:240]}). "
                "Report gated:captcha; do not treat the empty top-level page as "
                "gated:unknown."
            )
        elements = page.evaluate(_SNAPSHOT_JS)
        if not elements:
            return "(no interactive elements detected)"
        manual_artifacts = [
            str(element.get("label") or "")
            for element in elements
            if element.get("required") and any(
                phrase in str(element.get("label") or "").casefold()
                for phrase in (
                    "record a video", "video response", "video introduction",
                    "link to your video", "video link",
                )
            )
        ]
        if manual_artifacts:
            return (
                "GATE DETECTED: MANUAL ARTIFACT required before form mutation: "
                + "; ".join(manual_artifacts[:5])
                + ". Report review:manual_artifact; do not fill the form."
            )
        lines = []
        for e in elements:
            req = " *" if e.get("required") else ""
            type_part = f"/{e['type']}" if e.get("type") else ""
            value = f' value={e["value"]!r}' if e.get("value") else ""
            lines.append(
                f"{e['ref']}: [{e['role']}{type_part}{req}] {e['label']!r}{value}"
            )
        text = "\n".join(lines)
        # Long-form ATS pages (Palantir Lever: 100+ controls) overflow 8k and
        # the truncation makes the model stall reasoning about what it can't
        # see (2026-07-10). 16k chars ≈ 4k tokens; with the sliding prompt
        # cache the marginal cost is cached-read rate, i.e. negligible.
        if len(text) > 16000:
            text = text[:16000] + f"\n... [truncated, {len(text)} chars total]"
        return text
    except Exception as e:
        return _err(f"snapshot: {type(e).__name__}: {e}")


# ── click / fill ───────────────────────────────────────────────────────────

def _click_with_overlay_fallback(page: Page, ref: str, timeout: int = 10000) -> str:
    """Click a ref; if an overlay (hCaptcha iframe, consent banner) intercepts
    the pointer, wait for it to settle, retry, then fall back to a
    programmatic el.click() which bypasses hit-testing. Lever renders its
    invisible hCaptcha over form controls, which made every radio click fail.
    """
    loc = _ref_locator(page, ref)
    if _should_humanize(page):
        _human_hover(page, loc)
    try:
        loc.click(timeout=timeout)
        return f"clicked {ref}"
    except Exception as e:
        if "intercepts pointer events" not in str(e):
            raise
    time.sleep(2)
    try:
        loc.click(timeout=5000)
        return f"clicked {ref} (after overlay settled)"
    except Exception:
        loc.evaluate("el => el.click()")
        return f"clicked {ref} via JS (overlay was intercepting the pointer)"


def _is_submit_control(page: Page, ref: str) -> bool:
    return bool(_ref_locator(page, ref).evaluate(
        """el => {
            const tag = el.tagName.toLowerCase();
            const type = (el.type || '').toLowerCase();
            const text = (el.innerText || el.value || '').trim().toLowerCase();
            return (
                (tag === 'input' && (type === 'submit' || type === 'image')) ||
                (tag === 'button' && /\\b(submit|apply|send)\\b/.test(text))
            );
        }""",
        # Remote CDP round trips can exceed two seconds even on a healthy
        # Bright Data session. This is a safety check before every click, so a
        # premature timeout prevents the click rather than merely slowing it.
        timeout=8000,
    ))


def _profile_click_guard(
    page: Page, ref: str, profile: dict[str, Any] | None,
) -> str | None:
    if not profile:
        return None
    preferences = profile.get("employment_preferences") or {}
    if not (
        preferences.get("willing_to_relocate") is True
        and preferences.get("willing_to_work_onsite") is True
    ):
        return None
    loc = _ref_locator(page, ref)
    question = str(loc.get_attribute("data-applyd-question") or "").casefold()
    option = " ".join(
        re.sub(
            r"[^a-z0-9]+", " ",
            str(loc.get_attribute("data-applyd-option") or "").casefold(),
        ).split()
    )
    onsite_question = any(phrase in question for phrase in (
        "work from our", "work from the", "work onsite", "work on site",
        "in office", "in-office", "relocate",
    ))
    negative_option = option in {"no", "nope", "not willing", "unable"} or option.startswith("no ")
    if onsite_question and negative_option:
        return _err(
            f"click {ref}: profile says willing to relocate and work onsite; "
            f"refused contradictory option {option!r}"
        )
    return None


def click(
    page: Page, ref: str, profile: dict[str, Any] | None = None,
) -> str:
    try:
        guard = _profile_click_guard(page, ref, profile)
        if guard:
            return guard
        if _is_submit_control(page, ref):
            return _err(f"click {ref}: submit controls must use the submit tool")
        return _ok(_click_with_overlay_fallback(page, ref))
    except Exception as e:
        return _err(f"click {ref}: {type(e).__name__}: {e}")


def _grounded_fill_value(
    page: Page, ref: str, value: str, profile: dict[str, Any] | None,
) -> tuple[str, str | None]:
    """Bind consequential date fields to profile data, not model guesses."""
    if not profile or str(profile.get("earliest_start_date", "")).casefold() not in {
        "now", "immediately", "available now",
    }:
        return value, None
    loc = _ref_locator(page, ref)
    label = loc.evaluate(
        """el => {
            const direct = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
            return (direct?.innerText || el.closest('label')?.innerText ||
                el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                el.getAttribute('name') || '').trim();
        }""",
        timeout=3000,
    )
    normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", label.casefold()).split())
    if any(phrase in normalized for phrase in (
        "when can you start", "available to start", "availability date",
        "employment start date", "job start date",
    )):
        grounded = datetime.now().date().isoformat()
        return grounded, f"grounded start date from profile: {grounded}"
    return value, None


def fill(
    page: Page, ref: str, value: str, profile: dict[str, Any] | None = None,
) -> str:
    try:
        loc = _ref_locator(page, ref)
        if loc.get_attribute("role") == "combobox":
            return _err(
                f"fill {ref}: comboboxes require open_dropdown/pick_option or "
                "fill_autocomplete so the hidden selection is preserved"
            )
        value, grounding_note = _grounded_fill_value(page, ref, value, profile)
        if _should_humanize(page):
            _human_type(loc, value)
        else:
            loc.fill(value, timeout=10000)
        suffix = f"; {grounding_note}" if grounding_note else ""
        return _ok(f"filled {ref} ({len(value)} chars){suffix}")
    except Exception as e:
        return _err(f"fill {ref}: {type(e).__name__}: {e}")


def fill_many(
    page: Page, fields: list[dict[str, str]], profile: dict[str, Any] | None = None,
) -> str:
    out = []
    for i, f in enumerate(fields):
        ref = f.get("ref", "")
        val = f.get("value", "")
        try:
            loc = _ref_locator(page, ref)
            if loc.get_attribute("role") == "combobox":
                out.append(
                    f"  err: {ref} :: combobox requires open_dropdown/pick_option "
                    "or fill_autocomplete"
                )
                continue
            val, grounding_note = _grounded_fill_value(page, ref, val, profile)
            if _should_humanize(page):
                _human_type(loc, val)
                # Brief think-time between fields — but not after the last one.
                if i < len(fields) - 1:
                    time.sleep(_jitter(_THINK_BETWEEN_FIELDS))
            else:
                loc.fill(val, timeout=8000)
            suffix = f"; {grounding_note}" if grounding_note else ""
            out.append(f"  ok: {ref} ({len(val)} chars){suffix}")
        except Exception as e:
            out.append(f"  err: {ref} :: {type(e).__name__}: {e}")
    return f"fill_many ({len(fields)} fields):\n" + "\n".join(out)


def click_many(
    page: Page, refs: list[str], profile: dict[str, Any] | None = None,
) -> str:
    out = []
    for ref in refs:
        try:
            guard = _profile_click_guard(page, ref, profile)
            if guard:
                out.append(f"  err: {ref} :: {guard.removeprefix('error: ')}")
                continue
            if _is_submit_control(page, ref):
                out.append(f"  err: {ref} :: submit controls must use the submit tool")
                continue
            out.append(f"  ok: {_click_with_overlay_fallback(page, ref, timeout=8000)}")
        except Exception as e:
            out.append(f"  err: {ref} :: {type(e).__name__}: {e}")
    return f"click_many ({len(refs)} items):\n" + "\n".join(out)


def fill_autocomplete(page: Page, ref: str, value: str) -> str:
    """Fill a typeahead field (Lever location, Google Places, etc.) where a
    suggestion must be PICKED, not just typed — these widgets keep the real
    value in a hidden input (e.g. Lever's `selectedLocation`) and clear the
    visible field on submit if nothing was selected. Types keystroke-by-
    keystroke so suggestion listeners fire, then clicks the first suggestion
    (keyboard ArrowDown+Enter as fallback).
    """
    try:
        loc = _ref_locator(page, ref)
        # Lever's location typeahead is not an ARIA combobox. Its own script
        # searches on keydown and clears the visible field on blur unless it
        # also writes a JSON location object into #selected-location. The
        # generic click path is brittle because the result nodes have no ARIA
        # role and are destroyed on blur. Resolve through Lever's same-origin
        # endpoint and set the two fields exactly as its mousedown handler does.
        if loc.evaluate("el => el.classList.contains('location-input')"):
            selected = page.evaluate(
                """async ({ref, value}) => {
                    const input = document.querySelector(`[data-applyd-ref="${ref}"]`);
                    const hidden = document.querySelector('#selected-location');
                    if (!input || !hidden) return {error: 'Lever location fields missing'};
                    const token = document.querySelector('#hcaptchaResponseInput')?.value || '';
                    const url = '/searchLocations?text=' + encodeURIComponent(value) +
                        '&hcaptchaResponse=' + encodeURIComponent(token);
                    const response = await fetch(url, {credentials: 'same-origin'});
                    if (!response.ok) return {error: `location search returned HTTP ${response.status}`};
                    const choices = await response.json();
                    if (!Array.isArray(choices) || choices.length === 0) {
                        return {error: `no Lever location matched ${value}`};
                    }
                    const wanted = value.trim().toLowerCase();
                    const choice = choices.find(item =>
                        String(item.name || '').toLowerCase() === wanted
                    ) || choices.find(item =>
                        String(item.name || '').toLowerCase().startsWith(wanted)
                    ) || choices[0];
                    input.value = choice.name;
                    hidden.value = JSON.stringify(choice);
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    hidden.dispatchEvent(new Event('input', {bubbles: true}));
                    hidden.dispatchEvent(new Event('change', {bubbles: true}));
                    return {name: choice.name, hidden: hidden.value};
                }""",
                {"ref": ref, "value": value},
            )
            if selected.get("error"):
                return _err(f"fill_autocomplete {ref}: {selected['error']}")
            return _ok(
                f"fill_autocomplete {ref}: picked Lever location "
                f"{selected['name']!r} with hidden selection"
            )
        loc.click(timeout=5000)
        loc.fill("", timeout=5000)
        loc.press_sequentially(value, delay=80, timeout=20000)
        page.wait_for_timeout(1500)  # let async suggestions render
        options = page.evaluate(_OPTIONS_JS)
        if options:
            _ref_locator(page, options[0]["ref"]).click(timeout=5000)
            picked = repr(options[0]["text"])
        else:
            loc.press("ArrowDown")
            loc.press("Enter")
            picked = "keyboard ArrowDown+Enter (no suggestion list detected)"
        page.wait_for_timeout(500)
        final = (loc.evaluate("el => el.value") or "").strip()
        if not final:
            return _err(
                f"fill_autocomplete {ref}: field is empty after picking — "
                f"suggestions likely never rendered. Do not plain-fill this field."
            )
        return _ok(f"fill_autocomplete {ref}: picked {picked}; field value={final!r}")
    except Exception as e:
        return _err(f"fill_autocomplete {ref}: {type(e).__name__}: {e}")


# ── dropdown / combobox (two-step) ─────────────────────────────────────────

_OPTIONS_JS = r"""() => {
    // Clear stale option refs (those starting with 'o') from prior open_dropdown calls.
    document.querySelectorAll('[data-applyd-ref^="o"]').forEach(el => el.removeAttribute('data-applyd-ref'));

    let counter = 0;
    const out = [];
    // Common ARIA + library shapes
    const elements = document.querySelectorAll('[role="option"], [role="listbox"] li, .pac-item, .dropdown-location, [role="menuitem"]');
    for (const el of elements) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) continue;
        if (el.getAttribute('aria-hidden') === 'true') continue;
        const ref = `o${counter++}`;
        el.setAttribute('data-applyd-ref', ref);
        const text = (el.innerText || el.textContent || '').trim().slice(0, 120);
        out.push({ ref, text });
    }
    return out;
}"""


def open_dropdown(page: Page, ref: str) -> str:
    """Open a dropdown/combobox and return options with their own refs.

    Handles two cases:
    - Native <select>: read <option> children directly without click.
    - ARIA combobox / custom: click trigger, wait for listbox to render,
      mint refs for each [role=option] / li / .pac-item.
    """
    try:
        loc = _ref_locator(page, ref)
        tag = loc.evaluate("el => el.tagName.toLowerCase()", timeout=2000)
        if tag == "select":
            options = loc.evaluate(
                """el => {
                    document.querySelectorAll('[data-applyd-ref^="o"]').forEach(e => e.removeAttribute('data-applyd-ref'));
                    return Array.from(el.options).map((o, i) => {
                        o.setAttribute('data-applyd-ref', `o${i}`);
                        return { ref: `o${i}`, text: (o.text || o.value || '').trim() };
                    });
                }"""
            )
            if not options:
                return _err(f"open_dropdown {ref}: native <select> has no options")
            lines = "\n".join(f"  {o['ref']}: {o['text']}" for o in options)
            return f"opened {ref} (native select), {len(options)} options:\n{lines}"

        # ARIA combobox: click to expand, then read options. Mark this control
        # as the one being opened so pick_option can stamp the chosen value
        # back onto it (see _SNAPSHOT_JS / pick_option).
        loc.evaluate(
            """el => {
                document.querySelectorAll('[data-applyd-combobox-open]')
                    .forEach(e => e.removeAttribute('data-applyd-combobox-open'));
                el.setAttribute('data-applyd-combobox-open', '1');
            }"""
        )
        role = loc.get_attribute("role")
        if tag == "input" and role == "combobox":
            toggle = loc.locator("xpath=following-sibling::button").first
            if toggle.count():
                toggle.click(timeout=5000)
            else:
                loc.click(timeout=5000)
        else:
            loc.click(timeout=5000)
        page.wait_for_timeout(500)  # let async listbox render
        options = page.evaluate(_OPTIONS_JS)
        if not options:
            return _err(
                f"open_dropdown {ref}: clicked but no options rendered. "
                f"Trigger may not be a real dropdown, or listbox didn't open."
            )
        lines = "\n".join(f"  {o['ref']}: {o['text']}" for o in options)
        return f"opened {ref} ({len(options)} options):\n{lines}"
    except Exception as e:
        return _err(f"open_dropdown {ref}: {type(e).__name__}: {e}")


def pick_option(page: Page, option_ref: str) -> str:
    """Click an option previously surfaced by open_dropdown."""
    try:
        loc = _ref_locator(page, option_ref)
        tag = loc.evaluate("el => el.tagName.toLowerCase()", timeout=2000)
        if tag == "option":
            # Native <option>: set parent <select> value + dispatch events
            loc.evaluate(
                """el => {
                    const sel = el.parentElement;
                    sel.value = el.value;
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                    sel.dispatchEvent(new Event('input', { bubbles: true }));
                }"""
            )
            return _ok(f"picked native option {option_ref}")
        option_text = (loc.inner_text(timeout=2000) or "").strip()[:80]
        loc.click(timeout=5000)
        # Stamp the chosen value on the combobox open_dropdown marked, so the
        # next snapshot reports the field as filled. React comboboxes keep the
        # selection in component state (not el.value), so without this the field
        # reads back empty and the agent re-picks it forever (Greenhouse country
        # combobox burned a full 40-turn run this way).
        if option_text:
            page.evaluate(
                """(text) => {
                    const cb = document.querySelector('[data-applyd-combobox-open]');
                    if (cb) {
                        cb.setAttribute('data-applyd-picked', text);
                        cb.removeAttribute('data-applyd-combobox-open');
                    }
                }""",
                option_text,
            )
        return _ok(f"picked {option_ref} ({option_text!r})")
    except Exception as e:
        return _err(f"pick_option {option_ref}: {type(e).__name__}: {e}")


def _normalize_option_text(value: str) -> str:
    """Normalize display labels without erasing meaningful answer content."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def _match_option(options: list[dict[str, str]], desired: str) -> dict[str, str] | None:
    """Choose an exact label, or one uniquely compatible partial label.

    The partial case covers labels such as profile value ``Black`` versus ATS
    option ``Black or African American``. Ambiguity is deliberately returned
    as no match so legal/demographic answers are never guessed.
    """
    wanted = _normalize_option_text(desired)
    if not wanted:
        return None
    exact = [o for o in options if _normalize_option_text(o.get("text", "")) == wanted]
    if len(exact) == 1:
        return exact[0]
    compatible = []
    for option in options:
        text = _normalize_option_text(option.get("text", ""))
        if wanted in text or text in wanted:
            compatible.append(option)
    return compatible[0] if len(compatible) == 1 else None


def _read_options(page: Page) -> list[dict[str, str]]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('[data-applyd-ref^="o"]'))
            .map(el => ({
                ref: el.getAttribute('data-applyd-ref'),
                text: (el.innerText || el.textContent || el.value || '').trim()
            }))"""
    )


def _select_profile_guard(
    page: Page,
    ref: str,
    value: str,
    profile: dict[str, Any] | None,
    resume_text: str,
    job_locations: list[str] | None,
) -> str | None:
    """Reject consequential dropdown claims unsupported by structured facts."""
    if not profile:
        return None
    loc = _ref_locator(page, ref)
    label = " ".join(
        re.sub(
            r"[^a-z0-9]+", " ",
            str(loc.get_attribute("data-applyd-label") or "").casefold(),
        ).split()
    )
    desired = _normalize_option_text(value)
    location_text = " ".join(job_locations or []).casefold()
    uk_question = (
        any(term in label for term in (" uk ", "united kingdom", "britain"))
        or any(term in location_text for term in ("united kingdom", " uk", "london"))
    )
    legal_question = any(term in label for term in (
        "legally permitted to work", "authorized to work", "authorised to work",
        "work permit", "need a visa", "require sponsorship", "visa sponsorship",
    ))
    if uk_question and legal_question:
        uk = (profile.get("work_authorization") or {}).get("UK")
        if not isinstance(uk, dict):
            return _err(
                f"select_option {ref}: UK authorization/sponsorship is not in the "
                "structured profile; send to review instead of selecting an option"
            )

    if desired in {"yes", "true"}:
        evidence = (resume_text + " " + str(profile)).casefold()
        capability_groups = (
            (("profit and loss", "p l statements", "accounting principles"),
             ("profit and loss", "p&l", "accounting")),
            (("excel",), ("excel",)),
        )
        for question_terms, evidence_terms in capability_groups:
            if any(term in label for term in question_terms) and not any(
                term in evidence for term in evidence_terms
            ):
                return _err(
                    f"select_option {ref}: refused unsupported Yes; profile/resume "
                    f"contains no evidence for {label!r}"
                )
    return None


def select_option(
    page: Page,
    ref: str,
    value: str,
    profile: dict[str, Any] | None = None,
    resume_text: str = "",
    job_locations: list[str] | None = None,
) -> str:
    """Open a dropdown, deterministically match ``value``, and select it.

    This collapses the common open/inspect/pick sequence into one model call.
    On an ambiguous or absent match, it leaves the field untouched and returns
    the real choices so the agent can decide explicitly on its next turn.
    """
    try:
        guard = _select_profile_guard(
            page, ref, value, profile, resume_text, job_locations
        )
        if guard:
            return guard
        opened = open_dropdown(page, ref)
        if opened.startswith("error:"):
            return opened.replace("open_dropdown", "select_option", 1)
        # Inspect what the component actually rendered before typing anything.
        # Most answer dropdowns are short, and an exact/unique compatible choice
        # should be selected directly from their real labels.
        initial_options = _read_options(page)
        options = initial_options
        matched = _match_option(options, value)

        # Searchable React/Greenhouse lists virtualize thousands of choices;
        # clicking alone exposes only the first screen (Aalborg, Aalto, ...),
        # so only when the intended value is absent do we search and inspect
        # the filtered real options again.
        searched = False
        if matched is None:
            loc = _ref_locator(page, ref)
            tag = loc.evaluate("el => el.tagName.toLowerCase()", timeout=8000)
            role = loc.get_attribute("role")
            if tag == "input" and role == "combobox":
                loc.fill(value, timeout=8000)
                page.wait_for_timeout(650)
                page.evaluate(_OPTIONS_JS)
                options = _read_options(page)
                matched = _match_option(options, value)
                searched = True
        if matched is None:
            visible = ", ".join(repr(o.get("text", "")) for o in initial_options[:30])
            filtered = ", ".join(repr(o.get("text", "")) for o in options[:30])
            search_note = f"; filtered options after search: {filtered}" if searched else ""
            return _err(
                f"select_option {ref}: no unambiguous match for {value!r}; "
                f"initial visible options: {visible}{search_note}. "
                "Inspect these labels and call open_dropdown on the next turn "
                "before choosing a different answer."
            )
        result = pick_option(page, matched["ref"])
        if result.startswith("error:"):
            return result.replace("pick_option", "select_option", 1)
        # Dropdown components normally rerender only themselves. Do not call
        # snapshot here: snapshot clears every existing r* ref and would make
        # the remaining independent select_option calls in this same model
        # turn stale. If an ATS performs a whole-form rerender, the affected
        # later action fails cleanly and the agent snapshots on its next turn.
        page.wait_for_timeout(250)
        return _ok(f"select_option {ref}: selected {matched['text']!r}")
    except Exception as exc:
        return _err(f"select_option {ref}: {type(exc).__name__}: {exc}")


# ── upload ─────────────────────────────────────────────────────────────────

def upload_file(page: Page, ref: str, file_path: str) -> str:
    p = Path(file_path)
    if not p.exists():
        return _err(f"upload_file: file not found {file_path}")
    try:
        loc = _ref_locator(page, ref)
        is_file_input = loc.evaluate(
            "el => el.tagName.toLowerCase() === 'input' && el.type === 'file'", timeout=2000
        )
        if is_file_input:
            loc.set_input_files(str(p), timeout=10000)
            return _ok(f"uploaded {p.name} → {ref}")
        # Drop-zone: walk to the nearest input[type=file] in the same form/section
        file_input_handle = loc.evaluate_handle(
            """el => {
                const scope = el.closest('form, [role="dialog"], section, fieldset, div') || document;
                return scope.querySelector('input[type="file"]');
            }"""
        )
        if not file_input_handle:
            return _err(f"upload_file {ref}: no input[type=file] found near this ref")
        file_input_handle.as_element().set_input_files(str(p), timeout=10000)
        return _ok(f"uploaded {p.name} via input near {ref}")
    except Exception as e:
        return _err(f"upload_file {ref}: {type(e).__name__}: {e}")


def upload_cover_letter(
    page: Page,
    ref: str,
    content: str,
    profile: dict[str, Any] | None = None,
    company: str = "",
    title: str = "",
) -> str:
    """Render model-authored prose into a runner-owned PDF and upload it."""
    normalized = content.strip().replace("—", ",")
    words = normalized.split()
    if not 60 <= len(words) <= 350:
        return _err(
            f"upload_cover_letter: expected 60-350 words, received {len(words)}"
        )
    profile = profile or {}
    name = escape_latex(profile.get("full_name") or "Applicant")
    email = escape_latex(profile.get("email") or "")
    phone = escape_latex(profile.get("phone") or "")
    company_text = escape_latex(company or "Hiring Team")
    title_text = escape_latex(title or "Application")
    date_text = datetime.now().strftime("%B %d, %Y")
    paragraphs = [
        escape_latex(part.strip())
        for part in re.split(r"\n\s*\n", normalized)
        if part.strip()
    ]
    body = "\n\n\\par\n\n".join(paragraphs)
    latex = rf"""\documentclass[11pt]{{article}}
\usepackage[margin=1in]{{geometry}}
\usepackage[T1]{{fontenc}}
\usepackage{{lmodern}}
\usepackage{{parskip}}
\pagestyle{{empty}}
\begin{{document}}
{{\Large\textbf{{{name}}}}}\\
{email}{(" $|$ " + phone) if phone else ""}

\vspace{{1.5em}}
{date_text}\\
Hiring Team\\
{company_text}\\
Re: {title_text}

\vspace{{1em}}
Dear Hiring Team,

{body}

Sincerely,\\
{name}
\end{{document}}
"""
    try:
        with tempfile.TemporaryDirectory(prefix="applyd-cover-letter-") as temp:
            temp_path = Path(temp)
            tex_path = temp_path / "cover-letter.tex"
            pdf_path = temp_path / "cover-letter.pdf"
            tex_path.write_text(latex, encoding="utf-8")
            result = subprocess.run(
                ["tectonic", "--outdir", str(temp_path), str(tex_path)],
                capture_output=True,
                text=True,
                timeout=45,
            )
            if result.returncode != 0 or not pdf_path.exists():
                return _err(
                    "upload_cover_letter: PDF compile failed: "
                    + (result.stderr or result.stdout)[-300:]
                )
            uploaded = upload_file(page, ref, str(pdf_path))
            if uploaded.startswith("ok:"):
                return _ok(f"generated and uploaded cover-letter.pdf → {ref}")
            return uploaded
    except Exception as exc:
        return _err(
            f"upload_cover_letter {ref}: {type(exc).__name__}: {str(exc)[:240]}"
        )


# ── submit ─────────────────────────────────────────────────────────────────

# How long to wait after a submit click for an invisible captcha to resolve.
# Lever's flow: clicking submit calls hcaptcha.execute(); the challenge can
# take 40s+ to clear (observed live) before Bright Data's solver grants the
# h-captcha-response token and the form auto-submits. Bailing in 2s guaranteed
# a false captcha skip on every Lever form.
SUBMIT_CAPTCHA_WAIT_SECONDS = 90

_CAPTCHA_STATE_JS = r"""() => {
    const frame = document.querySelector('iframe[src*="hcaptcha"], iframe[src*="recaptcha"]');
    const tok = document.querySelector('[name="h-captcha-response"], [name="g-recaptcha-response"]');
    return {
        present: !!frame,
        token_len: tok && tok.value ? tok.value.length : 0,
    };
}"""


def _solve_brightdata_captcha(page: Page) -> tuple[str, str]:
    """Invoke Bright Data's documented custom CDP CAPTCHA solver.

    This is intentionally separate from ordinary DOM polling. It is called
    only after the user-authorized real submit click and only when the active
    browser provider is Bright Data.
    """
    try:
        session = page.context.new_cdp_session(page)
        response = session.send("Captcha.solve", {"detectTimeout": 30_000})
        status = str((response or {}).get("status") or "unknown").lower()
        return status, str(response or {})[:300]
    except Exception as exc:
        return "error", f"{type(exc).__name__}: {str(exc)[:240]}"

# Post-submit email-verification wall (invisible-reCAPTCHA low-score fallback).
# When Greenhouse scores a session as bot-like it doesn't hard-block — it emails
# a one-time security code and injects an inline code field on the SAME page (no
# navigation, no reCAPTCHA token), asking the applicant to "enter the code and
# resubmit". Left unhandled, the old poll waited for a token/nav that never came,
# timed out, and the model reported a phantom 'applied'. This JS both DETECTS the
# wall and locates the code input + resubmit button (tagging them with refs) so
# we can complete it in-session. HIGH PRECISION: require verify LANGUAGE and an
# actual code input — a plain "thanks, check your email" page has neither.
_VERIFY_WALL_JS = r"""() => {
    const body = document.body ? document.body.innerText.toLowerCase() : '';
    const phrases = [
        'security code', 'verification code', 'enter the code',
        'resubmit your application', 'verify your email', 'verify your identity',
        'we sent a code', "we've sent a code", 'code we sent',
    ];
    const phrase = phrases.find(p => body.includes(p)) || null;
    let input = document.querySelector(
        'input[autocomplete="one-time-code"], input[name*="code" i], ' +
        'input[id*="code" i], input[aria-label*="code" i], ' +
        'input[placeholder*="code" i]'
    );
    if (!input) {
        for (const lb of Array.from(document.querySelectorAll('label'))) {
            if (/security code|verification code|\bcode\b/i.test(lb.innerText || '')) {
                const forId = lb.getAttribute('for');
                const cand = forId ? document.getElementById(forId) : lb.querySelector('input');
                if (cand && cand.tagName === 'INPUT') { input = cand; break; }
            }
        }
    }
    let code_ref = null, submit_ref = null;
    if (input) { input.setAttribute('data-applyd-ref', 'vcode'); code_ref = 'vcode'; }
    const btn = Array.from(document.querySelectorAll('button, input[type=submit]'))
        .find(b => /resubmit|submit|verify|confirm/i.test(b.innerText || b.value || '')
                   && b.offsetParent !== null);
    if (btn) { btn.setAttribute('data-applyd-ref', 'vsubmit'); submit_ref = 'vsubmit'; }
    return { phrase: phrase, has_input: !!input, code_ref: code_ref, submit_ref: submit_ref };
}"""


def _detect_verification_wall(page: Page) -> dict | None:
    """Return {phrase, code_ref, submit_ref} if the email-verification wall is
    present, else None. Also dumps the DOM when APPLYD_DUMP_DOM_PATH is set."""
    try:
        st = page.evaluate(_VERIFY_WALL_JS)
    except Exception:
        return None
    if not (st.get("phrase") and st.get("has_input")):
        return None
    dump = os.environ.get("APPLYD_DUMP_DOM_PATH")
    if dump:
        try:
            with open(dump, "w") as f:
                f.write(page.content())
            print(f"[verify] dumped wall DOM to {dump} (url={page.url})", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[verify] dump failed: {e}", file=sys.stderr)
    return st


def _type_code(page: Page, loc, code: str) -> None:
    """Type the security code as real keystrokes. Greenhouse's field is an
    8-box OTP widget (`security-input-0..7`, each maxlength=1) that auto-advances
    ONLY on key events — a plain .fill() would drop all but the first char, so
    this path is keystroke-based regardless of APPLYD_HUMANIZE."""
    loc.click(timeout=8000)
    try:
        loc.fill("")  # no-op on an empty OTP box; clears a single-input variant
    except Exception:
        pass
    page.keyboard.type(code, delay=random.randint(55, 120))


def complete_email_verification(page: Page, code: str, orig_ref: str | None = None) -> bool:
    """Enter the emailed security code into the inline field and resubmit, in the
    same live session. Returns True if the wall clears."""
    info = _detect_verification_wall(page)
    if not info or not info.get("code_ref"):
        print("[verify] no code field to fill", file=sys.stderr)
        return False
    loc = _ref_locator(page, info["code_ref"])
    _type_code(page, loc, code)
    time.sleep(_jitter((0.3, 0.9)))
    submit_ref = info.get("submit_ref") or orig_ref
    clicked = False
    if submit_ref:
        try:
            _click_with_overlay_fallback(page, submit_ref)
            clicked = True
        except Exception:
            clicked = False
    if not clicked:
        try:
            page.get_by_role(
                "button", name=re.compile("resubmit|submit|verify|confirm", re.I)
            ).first.click(timeout=8000)
            clicked = True
        except Exception:
            clicked = False
    if not clicked:
        print("[verify] could not click resubmit", file=sys.stderr)
        return False
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(3)
        if _detect_verification_wall(page) is None:
            print(f"[verify] wall cleared after code entry; url={page.url}", file=sys.stderr)
            return True
    print("[verify] wall still present after code entry (rejected/wrong field)", file=sys.stderr)
    return False


def _gated_wall_message(ref: str) -> str:
    return (
        f"submit {ref}: EMAIL VERIFICATION WALL detected. The ATS scored this "
        f"session as a bot and emailed a security code to the applicant; the "
        f"application is NOT finalized and no email reader is configured. Call "
        f"report_done with status='skipped' and note='gated:email_verification'."
    )


def _handle_wall(page: Page, ref: str, verify_ctx: dict | None, submitted_after) -> str:
    """Wall is up. Complete it in-session if a code reader is configured;
    otherwise report gated so the row isn't mislabeled 'applied'."""
    reader = (verify_ctx or {}).get("code_reader")
    company = (verify_ctx or {}).get("company")
    if reader is None:
        return _gated_wall_message(ref)
    print(f"[verify] wall detected; fetching code for {company!r}", file=sys.stderr)
    code = reader.fetch(company, submitted_after)
    if not code:
        return _err(
            f"submit {ref}: email-verification wall, but no security code arrived "
            f"in time. status='gated:email_verification'."
        )
    redacted = (code[:2] + "****") if len(code) > 2 else "****"
    if complete_email_verification(page, code, orig_ref=ref):
        return _ok(
            f"submission_confirmed via {ref} after email verification "
            f"(code {redacted}); url={page.url}"
        )
    return _err(
        f"submit {ref}: entered emailed code {redacted} but the wall did not clear. "
        f"status='gated:email_verification'."
    )


_SUBMISSION_RESULT_JS = r"""() => {
    const body = (document.body?.innerText || '').toLowerCase();
    const success = [
        'application submitted', 'application received', 'thanks for applying',
        'thank you for applying', 'we have received your application',
        'your application has been submitted', 'application was submitted'
    ].find(p => body.includes(p)) || null;
    const visible = el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
    };
    const errors = Array.from(document.querySelectorAll(
        '[aria-invalid="true"], [role="alert"], .error, .field-error, .validation-error'
    )).filter(visible).map(el => (el.innerText || el.getAttribute('aria-label') || '').trim())
      .filter(Boolean).slice(0, 5);
    const submitVisible = Array.from(document.querySelectorAll(
        'button[type="submit"], input[type="submit"]'
    )).some(visible);
    const formVisible = Array.from(document.querySelectorAll('form')).some(visible);
    return {success, errors, submitVisible, formVisible};
}"""


def _post_submit_result(page: Page, ref: str, verify_ctx: dict | None, submitted_after) -> str:
    if _detect_verification_wall(page) is not None:
        return _handle_wall(page, ref, verify_ctx, submitted_after)
    try:
        state = page.evaluate(_SUBMISSION_RESULT_JS)
    except Exception as exc:
        return _err(f"submit {ref}: could not verify result: {type(exc).__name__}: {exc}")
    if state.get("success"):
        return _ok(
            f"submission_confirmed via {ref}; marker={state['success']!r}; url={page.url}"
        )
    if state.get("errors"):
        return _err(
            f"submit {ref}: validation errors remain: {state['errors']}; application not confirmed"
        )
    if not state.get("formVisible") and not state.get("submitVisible"):
        return _ok(f"submission_confirmed via {ref}; form closed; url={page.url}")
    return _err(
        f"submit {ref}: submission outcome unconfirmed; form is still visible at {page.url}"
    )


def submit(page: Page, ref: str, test_mode: bool, verify_ctx: dict | None = None) -> str:
    try:
        control = _ref_locator(page, ref).evaluate(
            """el => ({
                tag: el.tagName.toLowerCase(),
                type: (el.type || '').toLowerCase(),
                text: (el.innerText || el.value || '').trim().toLowerCase(),
                inForm: !!el.closest('form')
            })""",
            timeout=8000,
        )
    except Exception as exc:
        return _err(f"submit {ref}: cannot inspect control: {type(exc).__name__}: {exc}")
    valid_control = (
        (control.get("tag") == "input" and control.get("type") in {"submit", "image"})
        or (
            control.get("tag") == "button"
            and re.search(r"\b(submit|apply|send)\b", control.get("text") or "")
        )
    )
    if not valid_control:
        return _err(
            f"submit {ref}: ref is not a recognized submit control; "
            "application not attempted"
        )
    if test_mode:
        return _ok(f"test_mode=true; would have clicked {ref}")
    try:
        if _should_humanize(page):
            time.sleep(_jitter(_THINK_BEFORE_SUBMIT))
        start_url = page.url
        submitted_after = datetime.now(timezone.utc)
        # Overlay fallback matters MOST here: Lever renders its invisible
        # hCaptcha widget over the submit button, so a bare click times out
        # (burned a fully-filled Palantir form, 2026-07-10). The JS-click
        # fallback fires the form's submit handler, which calls
        # hcaptcha.execute() — the captcha poll below then handles resolution.
        _click_with_overlay_fallback(page, ref)

        solver_status = None
        if (verify_ctx or {}).get("browser_provider") == "brightdata":
            solver_status, solver_detail = _solve_brightdata_captcha(page)
            print(
                f"[captcha] Bright Data explicit solver status={solver_status} "
                f"detail={solver_detail}",
                file=sys.stderr,
                flush=True,
            )
            # A solved challenge may auto-submit. Give the page a moment before
            # entering the normal evidence-based confirmation loop.
            if solver_status == "solve_finished":
                time.sleep(3)

        # Poll for ANY of three outcomes, checking the email-verify wall FIRST
        # every iteration (it appears inline with no navigation and no token, so
        # the old token/nav-only loop timed out on it and mislabeled 'applied').
        deadline = time.time() + SUBMIT_CAPTCHA_WAIT_SECONDS
        settled = False
        while time.time() < deadline:
            time.sleep(3)
            if _detect_verification_wall(page) is not None:
                return _handle_wall(page, ref, verify_ctx, submitted_after)
            if page.url.rstrip("/") != start_url.rstrip("/"):
                return _post_submit_result(page, ref, verify_ctx, submitted_after)
            state = page.evaluate(_CAPTCHA_STATE_JS)
            if not state.get("present"):
                # No captcha widget and no wall — a plain submit; short settle
                # then confirm.
                if not settled:
                    settled = True
                    continue
                return _post_submit_result(page, ref, verify_ctx, submitted_after)
            if state.get("token_len", 0) > 0:
                time.sleep(3)  # let the form's auto-submit fire
                return _post_submit_result(page, ref, verify_ctx, submitted_after)
        manual_wait = int(os.environ.get("APPLYD_CAPTCHA_MANUAL_WAIT_SECONDS", "0"))
        if manual_wait > 0:
            print(
                f"[captcha] automated resolution failed; complete the visible "
                f"challenge in Chrome within {manual_wait}s",
                file=sys.stderr,
                flush=True,
            )
            manual_deadline = time.time() + manual_wait
            while time.time() < manual_deadline:
                time.sleep(3)
                if _detect_verification_wall(page) is not None:
                    return _handle_wall(page, ref, verify_ctx, submitted_after)
                if page.url.rstrip("/") != start_url.rstrip("/"):
                    return _post_submit_result(page, ref, verify_ctx, submitted_after)
                state = page.evaluate(_CAPTCHA_STATE_JS)
                if state.get("token_len", 0) > 0:
                    time.sleep(3)
                    return _post_submit_result(page, ref, verify_ctx, submitted_after)
        # Timed out: last wall check, else report the stall.
        if _detect_verification_wall(page) is not None:
            return _handle_wall(page, ref, verify_ctx, submitted_after)
        solver_suffix = (
            f"; Bright Data solver status={solver_status}"
            if solver_status is not None else ""
        )
        return _err(
            f"submit {ref}: captcha did not resolve within "
            f"{SUBMIT_CAPTCHA_WAIT_SECONDS + manual_wait}s (no token, no navigation) — likely a "
            f"hard interactive challenge the solver can't clear{solver_suffix}"
        )
    except Exception as e:
        return _err(f"submit {ref}: {type(e).__name__}: {e}")


# ── dispatcher ─────────────────────────────────────────────────────────────

def dispatch(
    page: Page, name: str, args: dict[str, Any], *, test_mode: bool,
    verify_ctx: dict | None = None, job_url: str = "", resume_pdf_path: str = "",
    profile: dict[str, Any] | None = None,
    company: str = "", title: str = "",
    resume_text: str = "", job_locations: list[str] | None = None,
) -> str:
    if name == "navigate":
        return navigate(page, job_url)
    if name == "snapshot":
        return snapshot(page)
    if name == "click":
        return click(page, args["ref"], profile)
    if name == "fill":
        return fill(page, args["ref"], args["value"], profile)
    if name == "fill_many":
        return fill_many(page, args["fields"], profile)
    if name == "fill_autocomplete":
        return fill_autocomplete(page, args["ref"], args["value"])
    if name == "click_many":
        return click_many(page, args["refs"], profile)
    if name == "open_dropdown":
        return open_dropdown(page, args["ref"])
    if name == "pick_option":
        return pick_option(page, args["option_ref"])
    if name == "select_option":
        return select_option(
            page, args["ref"], args["value"], profile, resume_text, job_locations
        )
    if name == "upload_resume":
        result = upload_file(page, args["ref"], resume_pdf_path)
        if result.startswith("ok:"):
            # Resume autofill commonly re-renders Ashby/Greenhouse forms and
            # invalidates every prior ref. Remint refs immediately and make the
            # new canonical map part of this tool result.
            page.wait_for_timeout(750)
            result += "\nDOM refreshed after upload; use only these new refs:\n" + snapshot(page)
        return result
    if name == "upload_cover_letter":
        return upload_cover_letter(
            page, args["ref"], args["content"], profile, company, title
        )
    if name == "submit":
        result = submit(page, args["ref"], test_mode, verify_ctx=verify_ctx)
        if result.startswith("error:"):
            # ATS validation/spam errors commonly rerender the form and destroy
            # every data-applyd-ref. Returning a fresh snapshot prevents the
            # model from retrying a stale submit ref on the changed document.
            page.wait_for_timeout(500)
            result += "\nDOM refreshed after failed submit; use only these new refs:\n"
            result += snapshot(page)
        return result
    return _err(f"unknown tool: {name}")


# ── tool schema for OpenAI SDK (used through OpenRouter) ───────────────────

def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                **({"required": required} if required else {}),
            },
        },
    }


TOOL_DEFS = [
    _fn(
        "preflight",
        "Before changing a freshly snapshotted form, confirm you inspected every visible required question. List all required labels that are answerable and any consequential factual labels that are truly missing. Creative/motivation questions and referral-source questions are never missing facts.",
        {
            "answerable_required_labels": {
                "type": "array", "items": {"type": "string"},
                "description": "Every visible required label answerable from profile, resume, composition policy, or safe default.",
            },
            "missing_fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": [
                                "identity", "legal", "education", "employment",
                                "compensation", "demographic", "preference", "other_fact",
                            ],
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["label", "category", "reason"],
                },
            },
        },
        required=["answerable_required_labels", "missing_fields"],
    ),
    _fn(
        "navigate",
        "Open the runner-bound job URL. The URL cannot be supplied or changed by the model.",
        {},
    ),
    _fn(
        "snapshot",
        "Return a list of every visible interactive element on the current page with stable refs (r0, r1, ...). Each line is: 'rN: [role/type required-asterisk] \"label\" value=...'. ALWAYS call this before clicking or filling — refs come from here. Re-call after any action that materially changes the page.",
        {},
    ),
    _fn(
        "click",
        "Click an element by ref.",
        {"ref": {"type": "string"}},
        required=["ref"],
    ),
    _fn(
        "fill",
        "Type a value into ONE text input/textarea by ref. For multiple fields prefer fill_many.",
        {"ref": {"type": "string"}, "value": {"type": "string"}},
        required=["ref", "value"],
    ),
    _fn(
        "fill_many",
        "Fill multiple text fields in one call. Each field is {ref, value}. Returns per-field ok/err so you can retry only failures.",
        {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"ref": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["ref", "value"],
                },
            }
        },
        required=["fields"],
    ),
    _fn(
        "fill_autocomplete",
        "Fill a typeahead/suggestion field (location fields, Google Places, Lever location) where an option must be PICKED from a dropdown that appears as you type. Types the value keystroke-by-keystroke and selects the first suggestion. Use this INSTEAD of fill for any field that shows suggestions — plain fill leaves the hidden selection empty and the form clears the field on submit.",
        {"ref": {"type": "string"}, "value": {"type": "string"}},
        required=["ref", "value"],
    ),
    _fn(
        "click_many",
        "Click multiple elements (radio sets, checkbox sets) in one call. Each item is a ref.",
        {"refs": {"type": "array", "items": {"type": "string"}}},
        required=["refs"],
    ),
    _fn(
        "select_option",
        "Select a known dropdown/combobox answer in one call. The runner opens the field, searches virtualized lists, matches the requested visible label deterministically, and selects it. Batch independent select_option calls when their refs come from the same snapshot. If no unambiguous label matches, it returns the actual choices without guessing.",
        {"ref": {"type": "string"}, "value": {"type": "string"}},
        required=["ref", "value"],
    ),
    _fn(
        "open_dropdown",
        "Inspect choices only when the desired answer is genuinely unknown until you see the list. Never batch multiple open_dropdown calls: option refs would collide. For any known profile answer, use select_option instead. Returns NEW refs prefixed o, then call pick_option.",
        {"ref": {"type": "string"}},
        required=["ref"],
    ),
    _fn(
        "pick_option",
        "Click an option by its ref (returned from open_dropdown).",
        {"option_ref": {"type": "string"}},
        required=["option_ref"],
    ),
    _fn(
        "upload_resume",
        "Upload the runner-bound tailored resume. The ref can point to a file input or visible drop-zone.",
        {"ref": {"type": "string"}},
        required=["ref"],
    ),
    _fn(
        "upload_cover_letter",
        "Create a runner-owned PDF from grounded cover-letter prose and upload it to the referenced required cover-letter file field. Never use for an optional cover letter.",
        {"ref": {"type": "string"}, "content": {"type": "string"}},
        required=["ref", "content"],
    ),
    _fn(
        "submit",
        "Click the form's submit button by ref. In test mode this is a no-op (logged but not actually clicked).",
        {"ref": {"type": "string"}},
        required=["ref"],
    ),
    _fn(
        "report_done",
        "Final tool. Call exactly once at the end of the run. Status must be 'applied', 'review', 'skipped', or 'failed'. Use review:missing_info with the exact field label when a required factual answer is unavailable.",
        {
            "status": {"type": "string", "enum": ["applied", "review", "skipped", "failed"]},
            "note": {"type": "string"},
        },
        required=["status", "note"],
    ),
]
