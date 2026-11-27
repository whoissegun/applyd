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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from playwright.sync_api import Page, TimeoutError as PWTimeout

from ..tailor.structured import escape_latex
from . import option_semantics
from .deterministic_questions import (
    graduation_start_date, immigration_authorization_required_question,
    required_consent_authorized, resolve_question, resolve_text_question,
)


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

_US_STATE_CODES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|"
    "MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|"
    "SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
_CA_PROVINCE_CODES = "ON|BC|AB|QC|NS|NB|MB|SK|PE|NL|NT|NU|YT"


def _authorization_region_from_locations(
    job_locations: list[str] | None,
) -> str | None:
    """Resolve a legal-answer region from deterministic job locations."""
    location = " | ".join(job_locations or []).casefold()
    if job_locations and all(str(place).strip().casefold() in {"sf", "nyc", "south sf"} for place in job_locations):
        return "US"
    from .deterministic_questions import _US_STATE_NAMES
    if job_locations and all(str(p).strip().casefold() in _US_STATE_NAMES for p in job_locations):
        return "US"
    if (
        any(term in location for term in (
            "united states", "usa", "u.s.", "san francisco", "new york",
            "foster city", "palo alto", "california", "boston",
        ))
        or re.search(rf",\s*(?:{_US_STATE_CODES})(?:\b|$)", location, re.I)
    ):
        return "US"
    if (
        any(term in location for term in (
            "canada", "ontario", "toronto", "ottawa", "vancouver", "montreal",
        ))
        or re.search(rf",\s*(?:{_CA_PROVINCE_CODES})(?:\b|$)", location, re.I)
    ):
        return "CA"
    if (
        any(term in location for term in (
            "united kingdom", "england", "scotland", "wales", "london",
        ))
        or re.search(r"\buk\b", location)
    ):
        return "UK"
    return None


def _authorization_region_from_residence(profile: dict[str, Any]) -> str | None:
    """Resolve legal region from the candidate's residence, not the job."""
    code = " ".join(re.sub(
        r"[^a-z0-9]+", " ", str(profile.get("address_country_code") or "").casefold(),
    ).split())
    country = " ".join(re.sub(
        r"[^a-z0-9]+", " ", str(profile.get("address_country") or "").casefold(),
    ).split())
    aliases = {
        "ca": "CA", "canada": "CA",
        "us": "US", "usa": "US", "united states": "US",
        "united states of america": "US",
        "uk": "UK", "gb": "UK", "united kingdom": "UK",
    }
    return aliases.get(code) or aliases.get(country)


def _authorization_regions_from_text(text: str) -> list[str]:
    """Return every explicitly named legal jurisdiction, not just the first."""
    padded = " " + " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split()) + " "
    regions: list[str] = []
    if any(term in padded for term in (" united states ", " usa ", " u s ", " us ")):
        regions.append("US")
    if " canada " in padded or " canadian " in padded:
        regions.append("CA")
    if any(term in padded for term in (" united kingdom ", " uk ", " britain ")):
        regions.append("UK")
    if " european union " in padded or " eu " in padded:
        regions.append("EU")
    return regions


def _selected_application_locations(page: Page) -> list[str]:
    """Read already-selected work-location choices from the current form."""
    values = page.evaluate(r"""() => Array.from(document.querySelectorAll('[data-applyd-ref]'))
      .filter(e => {
        const q=((e.getAttribute('data-applyd-question') || '') + ' ' +
          (e.getAttribute('data-applyd-label') || '')).toLowerCase();
        // Legal questions ("authorized to work in the country you are
        // applying to?") mention country+work but their answer is Yes/No,
        // not a location.
        return /(?:location|countr(?:y|ies))/.test(q) &&
          /(?:work|selected|reside|residence|previous response)/.test(q) &&
          !/(?:authori[sz]|sponsor|visa|citizen|permit|eligib|legally)/.test(q) &&
          (!!e.checked || e.getAttribute('aria-checked') === 'true' || e.getAttribute('aria-pressed') === 'true' ||
           !!e.getAttribute('data-applyd-picked') ||
           (e.tagName === 'SELECT' && !!e.value));
      })
      .map(e => e.getAttribute('data-applyd-option') ||
        e.getAttribute('data-applyd-picked') ||
        (e.tagName === 'SELECT' ? (e.selectedOptions[0]?.text || e.value) : e.value) || '')
      .map(v => v.trim()).filter(Boolean)
      .filter(v => !/^(?:yes|no|true|false)$/i.test(v))""")
    return [str(value) for value in values] if isinstance(values, list) else []


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

def _clear_saved_form_state(page: Page, url: str) -> None:
    """Drop the application site's saved form drafts before loading it.

    The persistent Chrome profile keeps localStorage/IndexedDB between runs,
    and some ATS forms restore a previous (possibly wrong, test-mode) answer
    on load. Cookies are left alone so sign-ins survive.
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return
        origin = f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
        session = page.context.new_cdp_session(page)
        session.send("Storage.clearDataForOrigin", {
            "origin": origin,
            "storageTypes": "local_storage,session_storage,indexeddb,cache_storage,service_workers",
        })
        session.detach()
    except Exception:  # noqa: BLE001
        pass  # non-Chromium or remote browsers: best effort only


def navigate(page: Page, url: str, wait_ms: int = 30000) -> str:
    current = page.url or ""
    if current.rstrip("/") == url.rstrip("/"):
        return _err(
            f"navigate refused: already on {current}. Re-navigating would "
            f"clear the form. Use snapshot to re-inspect current state instead."
        )
    _clear_saved_form_state(page, url)
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
            // aria-labelledby is a space-separated id list (Workable's resume
            // input uses "<label-id> <description-id>").
            labeledBy = ariaby.trim().split(/\s+/)
                .map(id => (document.getElementById(id)?.innerText || '').trim())
                .filter(Boolean).join(' ');
        }
        const wrappedLabel = el.closest('label');
        const choiceRole = ['radio', 'checkbox'].includes(el.getAttribute('role'));
        const option = (labelEl?.innerText || wrappedLabel?.innerText ||
            (choiceRole ? el.querySelector('label')?.innerText : '') || '').trim();
        const fieldset = el.closest('fieldset');
        let questionEl = fieldset?.querySelector(
            ':scope > .ashby-application-form-question-title, :scope > legend'
        ) || null;
        // Workable points a radiogroup's aria-labelledby at a prompt outside
        // the fieldset. The individual radio's label is only YES / NO.
        if (!questionEl && fieldset?.getAttribute('aria-labelledby')) {
            const ids = fieldset.getAttribute('aria-labelledby').trim().split(/\s+/);
            if (ids.length === 1) questionEl = document.getElementById(ids[0]);
        }
        if (!questionEl && (!(inputType === 'radio' || inputType === 'checkbox') || !option)) {
            let questionScope = el.parentElement;
            for (let depth = 0; depth < 5 && questionScope; depth++, questionScope = questionScope.parentElement) {
                questionEl = questionScope.querySelector(':scope > .ashby-application-form-question-title');
                if (questionEl) break;
            }
        }
        if (!questionEl) {
            // Lever custom questions keep their prompt in a sibling
            // `.application-label`; the control itself only exposes an opaque
            // `cards[uuid][fieldN]` name. Recover the visible prompt so
            // preflight and filling are grounded in the actual question.
            const leverQuestion = el.closest('li.application-question');
            questionEl = leverQuestion?.querySelector('.application-label') || null;
        }
        let question = (questionEl?.innerText || '').trim();
        // A standalone privacy acknowledgement has no separate prompt: its
        // wrapping label is both the question and the checkbox option.
        if (!question && inputType === 'checkbox' &&
            /^by submitting,? i acknowledge my personal data will be processed in accordance with .+privacy policy\.?$/i
                .test(option.replace(/\s+/g, ' '))) question = option;
        const ph = el.getAttribute('placeholder') || '';
        const name = el.getAttribute('name') || '';
        let label = question || option || aria || labeledBy || ph || name || el.innerText || '';
        if ((inputType === 'radio' || inputType === 'checkbox' || choiceRole) && question && option && question !== option) {
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
        // Greenhouse renders every upload as a bare "Attach"; its id names
        // the slot (resume vs cover_letter). Without it the model cannot tell
        // the resume input from the cover-letter one.
        if (inputType === 'file' && /^(?:attach|choose file|upload|browse)\b/i.test(label.trim())) {
            const hint = (el.id || el.getAttribute('name') || '').replace(/[_-]+/g, ' ').trim();
            if (hint && !/^input files input/i.test(hint)) label = `${hint} — ${label}`;
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
        el.setAttribute('data-applyd-required', required ? 'true' : 'false');

        // React comboboxes (Greenhouse country/school/degree, react-select)
        // keep the chosen value in component state, not el.value — so a picked
        // field reads back empty and the agent re-picks it forever. pick_option
        // stamps the choice on data-applyd-picked; surface it as the value so
        // the field reads as satisfied. Survives snapshot (only *-ref is reset).
        const picked = el.getAttribute('data-applyd-picked');
        const selected = !!el.checked || el.getAttribute('aria-checked') === 'true' ||
                         el.getAttribute('aria-pressed') === 'true' || !!picked;
        const rawValue = (el.value !== undefined && el.value !== '')
            ? el.value : (el.innerText || '');
        const value = (picked || rawValue || '').toString().slice(0, 80);

        out.push({ ref, role, type: inputType, label, required, value, selected });
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
            selected = " selected" if e.get("selected") else ""
            type_part = f"/{e['type']}" if e.get("type") else ""
            value = f' value={e["value"]!r}' if e.get("value") else ""
            lines.append(
                f"{e['ref']}: [{e['role']}{type_part}{req}{selected}] {e['label']!r}{value}"
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
            const inForm = !!el.closest('form');
            return (
                (tag === 'input' && (type === 'submit' || type === 'image')) ||
                (tag === 'button' && (
                    /\\b(submit|send)\\b/.test(text) ||
                    (inForm && /\\bapply\\b/.test(text))
                ))
            );
        }""",
        # Remote CDP round trips can exceed two seconds even on a healthy
        # Bright Data session. This is a safety check before every click, so a
        # premature timeout prevents the click rather than merely slowing it.
        timeout=8000,
    ))


def _degree_claim_guard(question: str, value: str, profile: dict[str, Any]) -> str | None:
    """Independently reject unsupported degree-possession yes/no claims."""
    if not re.search(r'\b(?:bachelor|master)', question, re.I):
        return None
    if not re.search(r'\b(?:have|hold|earned|completed|obtained|possess|received)\b', question, re.I):
        return None
    desired = _normalize_option_text(value)
    actual = True if re.match(r'^(?:yes|true)\b', desired) else False if re.match(r'^(?:no|false)\b', desired) else None
    if actual is None:
        return None
    answer = resolve_question(question, profile, {'yes': 'Yes', 'no': 'No'})
    if not isinstance(answer.value, bool) or actual != answer.value:
        return _err('degree qualification answer is not supported by explicit credential completion; send to review')
    return None


def _denies_authorization(desired: str) -> bool:
    """Option text that denies current authorization in any wording.

    "I don't currently have the right to work in the UK and would need visa
    sponsorship" was read as claiming the right because only exact phrases
    were listed. A negation shortly before have/hold/right/authorized never
    claims current authorization. (Needing sponsorship alone does not excuse
    an option that also claims to hold a visa.)
    """
    text = " " + _normalize_option_text(desired) + " "
    return bool(re.search(
        r"\b(?:not|do not|don t|dont|does not|doesn t|cannot|can t|no|without|lack)\b"
        r"(?:\s+\w+){0,3}?\s+(?:currently\s+)?(?:have|hold|possess|authori[sz]ed|eligible|right|permitted)\b",
        text,
    ))


def _covenant_answer_ok(text: str, desired: str, profile: dict[str, Any]) -> bool:
    """A non-compete answer backed by the explicit restrictive-covenant fact."""
    background = profile.get("background_defaults") or {}
    fact = background.get("no_restrictive_covenants")
    if not isinstance(fact, bool) or not re.search(r"\bnon[ -]?(?:compete|competition|solicit)", text):
        return False
    if re.search(r"\bnon[ -]?disclosure|\bconfidentiality\b|\bnda\b", text):
        nda = background.get("no_confidentiality_agreements")
        if not isinstance(nda, bool):
            return False
        fact = fact and nda
    wanted = _normalize_option_text(desired)
    says_no = wanted in {"no", "false"} or wanted.startswith("no ")
    says_yes = wanted in {"yes", "true"} or wanted.startswith("yes ")
    return (fact and says_no) or (not fact and says_yes)


def _profile_click_guard(
    page: Page, ref: str, profile: dict[str, Any] | None,
    job_locations: list[str] | None = None,
) -> str | None:
    if not profile:
        return None
    loc = _ref_locator(page, ref)
    question = str(loc.get_attribute("data-applyd-question") or "").casefold()
    legal_label = question + ' ' + str(loc.get_attribute('data-applyd-label') or '').casefold()
    required = (loc.get_attribute("aria-required") == "true" or loc.get_attribute("required") is not None
                or loc.get_attribute("data-applyd-required") == "true")
    standing_consent = required_consent_authorized(legal_label, profile, required=required)
    if (not standing_consent and re.search(r'\b(?:arbitration|non[ -]?compete|waiv(?:e|er) of rights)\b', legal_label)
            and not _covenant_answer_ok(legal_label, str(loc.get_attribute("data-applyd-option") or ""), profile)):
        return _err(f'click {ref}: unusual legal attestation requires human review')
    option = " ".join(
        re.sub(
            r"[^a-z0-9]+", " ",
            str(loc.get_attribute("data-applyd-option") or "").casefold(),
        ).split()
    )
    degree_error = _degree_claim_guard(question, option, profile)
    if degree_error:
        return degree_error
    if (
        any(term in question for term in ("recording", "recorded", "record interview"))
        and any(term in question for term in ("consent", "agree", "permission"))
    ):
        consent = (profile.get("application_policy") or {}).get(
            "interview_recording_consent"
        )
        if standing_consent:
            consent = True
        if not isinstance(consent, bool):
            return _err(
                f"click {ref}: interview-recording consent is not in the "
                "structured profile; send to review"
            )
        says_yes = option in {"yes", "true"} or option.startswith("yes ")
        says_no = option in {"no", "false"} or option.startswith("no ")
        if (says_yes and not consent) or (says_no and consent):
            return _err(
                f"click {ref}: refused contradictory interview-recording consent"
            )
    preferences = profile.get("employment_preferences") or {}

    residence_relative = any(phrase in question for phrase in (
        "country you are based in", "country you re based in",
        "country where you are based", "country where you currently reside",
        "country you currently reside in", "your country of residence",
        "location you currently reside", "location where you currently reside",
    ))
    residence_question = not residence_relative and any(phrase in question for phrase in (
        "are you located in", "are you based in", "do you live in",
        "do you reside in", "currently reside in",
    ))
    if residence_question and option:
        profile_places = [
            " ".join(re.sub(r"[^a-z0-9]+", " ", str(profile.get(key) or "").casefold()).split())
            for key in ("address_city", "address_region", "address_country")
        ]
        profile_places = [place for place in profile_places if place]
        question_matches_profile = any(place in question for place in profile_places)
        relocation_alternative = (
            " or " in question
            and "relocat" in question
            and preferences.get("willing_to_relocate") is True
        )
        says_yes = option in {"yes", "true"} or option.startswith("yes ")
        says_no = option in {"no", "false"} or option.startswith("no ")
        if says_yes and not question_matches_profile and not relocation_alternative:
            return _err(
                f"click {ref}: refused unsupported residence claim {option!r}; "
                "the named place is not the structured profile city, region, or country"
            )
        if says_no and (question_matches_profile or relocation_alternative):
            return _err(
                f"click {ref}: refused false residence denial {option!r}; "
                "the question matches the structured profile location or relocation preference"
            )

    # Radio/checkbox options carry the same consequential claims as dropdown
    # options. Route them through the structured authorization/location guard
    # before clicking; otherwise a ref remap can turn the intended truthful
    # radio into an adjacent visa-status claim.
    if option:
        consequential = _select_profile_guard(
            page, ref, option, profile, "", job_locations
        )
        if consequential:
            return consequential.replace("select_option", "click", 1)

    if not (
        preferences.get("willing_to_relocate") is True
        and preferences.get("willing_to_work_onsite") is True
    ):
        return None
    onsite_question = any(phrase in question for phrase in (
        "work from our", "work from the", "work onsite", "work on site",
        "in office", "in-office", "relocate",
    ))
    negative_option = option in {"no", "nope", "not willing", "unable"} or option.startswith("no ")
    # "Are you currently located within commuting distance ... ?" asks a
    # fact; No is the truthful answer for a non-local candidate even though
    # they are willing to relocate (the question offers no relocation path).
    asks_current_location = bool(re.search(
        r"\bcurrently (?:located|live|living|based|reside|residing)\b|\b(?:located|live|living|reside|based) "
        r"within\b|\bcommuting distance\b|\bcommutable\b|\bare you local\b", question))
    if asks_current_location and not re.search(r"\b(?:willing|open) to relocat|\bif not\b", question):
        city = str(profile.get("address_city") or "").casefold()
        if city and city not in question:
            return None
    if onsite_question and negative_option:
        return _err(
            f"click {ref}: profile says willing to relocate and work onsite; "
            f"refused contradictory option {option!r}"
        )
    return None


def click(
    page: Page, ref: str, profile: dict[str, Any] | None = None,
    job_locations: list[str] | None = None,
) -> str:
    try:
        guard = _profile_click_guard(page, ref, profile, job_locations)
        if guard:
            return guard
        if _is_submit_control(page, ref):
            return _err(f"click {ref}: submit controls must use the submit tool")
        return _ok(_click_with_overlay_fallback(page, ref))
    except Exception as e:
        return _err(f"click {ref}: {type(e).__name__}: {e}")


def _narrative_question(label: str) -> bool:
    """Recognize open stories, not factual questions rendered as text boxes."""
    text = " ".join(re.sub(r"[^a-z0-9]+", " ", label.casefold()).split())
    if any(term in text for term in (
        "authoriz", "authoris", "sponsor", "visa", "citizen", "right to work",
        "eligible to work", "legal", "criminal", "clearance", "conflict of interest",
        "salary", "compensation", "hourly rate", "graduat", "gpa", "grade",
        "degree", "qualification", "certif", "licens", "proficien", "fluent",
        "years of", "how many", "employer", "employment", "work permit", "metric",
        "high school performance", "high school rank", "high school score",
    )):
        return False
    return any(term in text for term in (
        "why us", "why do you", "why are you", "why this", "what interests you",
        "what excites you", "tell us about", "tell me about", "describe a time",
        "example of a time", "favorite project", "favourite project", "project you",
        "proud of", "greatest strength", "taught yourself", "self taught",
        "learned on your own", "how do you stay current", "how do you keep current",
        "how do you keep up", "stay up to date", "you ve read", "you have read",
        "reading", "read in the past month", "technical topic", "motivation",
    ))


class _OptionalNarrativeField(ValueError):
    pass


def _grounded_fill_value(
    page: Page, ref: str, value: str, profile: dict[str, Any] | None,
    resume_text: str = "", job_locations: list[str] | None = None,
) -> tuple[str, str | None]:
    """Bind consequential text fields to profile data, not model guesses."""
    if not profile:
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
    evidence = " ".join(
        re.sub(
            r"[^a-z0-9]+", " ",
            (resume_text + " " + str(profile)).casefold(),
        ).split()
    )
    answer = " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())
    if 'employer' in normalized and any(term in normalized for term in ('current', 'most recent', 'previous')):
        if re.search(r'\b(?:student|studying)\b', answer):
            raise ValueError('student affiliation is not an employer; use source resume employment, not current_company')
    if 'graduat' in normalized and any(term in normalized for term in ('date', 'month', 'year')):
        from .deterministic_questions import resolve_text_question
        kind = loc.get_attribute('type') or 'text'
        mask = str(loc.get_attribute('placeholder') or '').casefold()
        if re.fullmatch(r'20\d{2}-\d{2}', str(profile.get('expected_grad_date') or '')) and (
                kind == 'date' or re.search(r'(?:mm|dd)[/-](?:mm|dd)[/-](?:yyyy|yy)', mask)):
            raise ValueError('day-specific graduation date required but only a month is recorded; send to review')
        mapped = resolve_text_question(label, profile, input_type=kind)
        if mapped is None or mapped.value is None:
            raise ValueError('graduation date is not mapped to a sufficiently precise profile value; send to review')
        return str(mapped.value), 'grounded graduation date from profile (no invented day)'
    narrative_authorized = False
    if _narrative_question(label):
        required = loc.evaluate(r"""e => e.getAttribute('data-applyd-required') === 'true' || !!e.required || e.getAttribute('aria-required') === 'true'
          || /\*|\brequired\b/i.test((e.getAttribute('data-applyd-label') || '') + ' ' +
            (e.getAttribute('data-applyd-question') || '') + ' ' +
            (e.id ? document.querySelector(`label[for="${e.id}"]`)?.innerText || '' : '') +
            ' ' + (e.closest('label')?.innerText || ''))""")
        if required is False:
            raise _OptionalNarrativeField('optional narrative field must stay blank')
        narrative_authorized = (required is True and
            (profile.get('application_policy') or {}).get('required_narrative_invention') is True)

    # A request for something the applicant actually read is historical,
    # not permission to compose a plausible reading story.
    if not narrative_authorized and re.search(r"\b(?:read|reading)\b", normalized) and any(term in normalized for term in (
        "past month", "last month", "recently", "you ve read", "you have read",
    )):
        saved = profile.get("recent_reading_response")
        exact = profile.get("historical_application_answers") or {}
        if not saved and isinstance(exact, dict):
            saved = next((v for k, v in exact.items() if isinstance(v, str)
                          and " ".join(re.sub(r"[^a-z0-9]+", " ", k.casefold()).split()) == normalized), None)
        if not isinstance(saved, str) or not saved.strip():
            raise ValueError("recent reading history is not in the profile; send this application to review")
        return saved, "user-confirmed reading history"

    # Creative prompts permit tone and motivation, not invented autobiography.
    # These timeline markers turn prose into a factual life-history claim and
    # therefore must already appear in the profile or resume.
    personal_history_markers = (
        "in high school", "during high school", "as a child", "growing up",
        "when i was", "in university", "during university", "in college",
        "during college",
    )
    unsupported_personal_history = [
        phrase for phrase in personal_history_markers
        if phrase in answer and phrase not in evidence
    ]
    if unsupported_personal_history and not narrative_authorized:
        raise ValueError(
            "free-form response contains unsupported autobiographical claims "
            f"{unsupported_personal_history!r}; use only profile/resume facts "
            "or send to review"
        )

    compensation_question = any(phrase in normalized for phrase in (
        "salary", "compensation", "pay expectation", "expected pay",
        "desired pay", "hourly rate", "expected rate", "desired rate",
    ))
    if compensation_question and answer in {
        "nan", "n a", "na", "none", "null", "undefined", "unknown", "tbd",
    }:
        raise ValueError(
            "invalid compensation placeholder; use the structured salary "
            "policy or send this application to review"
        )

    if "high school" in normalized and any(term in normalized for term in (
        "perform", "performance", "rank", "ranking", "score",
    )):
        performance = profile.get("high_school_performance")
        subject = (
            "mathematics" if any(term in normalized for term in ("math", "mathematics"))
            else "native_language" if "native language" in normalized
            else "evidence"
        )
        grounded = performance.get(subject) if isinstance(performance, dict) else None
        if not str(grounded or "").strip():
            raise ValueError(
                "high-school academic performance is not in the structured "
                "profile; send this application to review"
            )
        return str(grounded), "grounded high-school performance from profile"

    if any(phrase in normalized for phrase in (
        "taught yourself", "self taught", "learned on your own",
    )) and not narrative_authorized and not any(phrase in evidence for phrase in (
        "taught myself", "self taught", "learned on my own",
    )):
        raise ValueError(
            "self-directed learning history is not in the profile/resume; "
            "send this application to review"
        )
    if any(phrase in normalized for phrase in (
        "how do you stay current", "how do you keep current",
        "how do you keep up with", "stay up to date",
    )) and not narrative_authorized and not profile.get("professional_learning_sources"):
        raise ValueError(
            "professional learning sources are not in the structured profile; "
            "send this application to review"
        )
    if any(phrase in normalized for phrase in (
        "live production issue", "production incident", "client incident",
    )) and not any(phrase in evidence for phrase in (
        "production issue", "production incident", "on call", "client issue",
        "incident response",
    )):
        raise ValueError(
            "live production/client incident history is not in the profile/resume; "
            "send this application to review"
        )
    historical_prompt = any(phrase in normalized for phrase in (
        "your experience", "example of a time", "describe a time",
        "technical topic", "project you", "system you", "issue you",
    ))
    unsupported_history_markers = (
        "daily", "every day", "weekly", "monthly", "last summer", "last year",
        "on call", "i follow", "i regularly", "recently",
    )
    if historical_prompt and not narrative_authorized:
        unsupported = [
            phrase for phrase in unsupported_history_markers
            if phrase in answer and phrase not in evidence
        ]
        if unsupported:
            raise ValueError(
                "historical response contains unsupported chronology, frequency, "
                f"or habit claims {unsupported!r}; use only profile/resume facts "
                "or send to review"
            )

    if any(phrase in normalized for phrase in (
        "experience using ai tools", "ai tools", "code generation", "copilot",
    )):
        named_tools = (
            "claude code", "claude api", "github copilot", "chatgpt",
            "cursor", "gemini", "windsurf",
        )
        unsupported_tools = [
            tool for tool in named_tools if tool in answer and tool not in evidence
        ]
        unsupported_frequency = [
            phrase for phrase in ("daily", "every day", "weekly", "monthly")
            if phrase in answer and phrase not in evidence
        ]
        if unsupported_tools or unsupported_frequency:
            unsupported = unsupported_tools + unsupported_frequency
            raise ValueError(
                "AI-tool experience contains unsupported historical claims "
                f"{unsupported!r}; use only product names and frequencies stated "
                "in the profile/resume, or send to review"
            )

    if any(phrase in normalized for phrase in (
        "street address", "address line 1", "mailing address",
    )):
        address = str(profile.get("address_line1") or "").strip()
        if address:
            return address, "grounded street address from profile"

    if "current company" in normalized:
        current_company = str(profile.get("current_company") or "").strip()
        if current_company:
            return current_company, "grounded current affiliation from profile"

    # Some Greenhouse forms render a required Yes/No sponsorship question as
    # a plain text input. Bind it to the regional structured record instead of
    # accepting model-authored text.
    if ((immigration_authorization_required_question(label) and 'sponsor' not in normalized) or any(phrase in normalized for phrase in (
        'authorized to work', 'authorised to work', 'eligible to work', 'right to work',
        'require sponsorship', 'need sponsorship',
    ))) and not any(phrase in normalized for phrase in (
        'explain', 'provide details', 'describe', 'what type', 'which visa',
    )):
        from .deterministic_questions import resolve_text_question
        mapped = resolve_text_question(label, profile, job_locations=job_locations or ())
        if mapped is not None:
            if mapped.value is None:
                raise ValueError('legal answer cannot be grounded: ' + mapped.reason)
            return str(mapped.value), 'grounded legal text answer from structured regional facts'
    sponsorship_yes_no = (
        "sponsor" in normalized
        and re.search(
            r"\b(?:require|requires|required|need|needs)\b"
            r"(?:\s+\w+){0,5}\s+sponsor\w*",
            normalized,
        )
        and not any(phrase in normalized for phrase in (
            "explain", "provide details", "describe", "what type", "which visa",
        ))
    )
    if sponsorship_yes_no:
        padded = f" {normalized} "
        region = None
        if any(term in padded for term in (
            " united states ", " u s ", " usa ", " h1b ", " h 1b ",
        )):
            region = "US"
        elif "canada" in normalized or "canadian" in normalized:
            region = "CA"
        elif any(term in padded for term in (
            " uk ", " united kingdom ", " britain ", " british ",
        )):
            region = "UK"
        elif "european union" in normalized or " eu " in padded:
            region = "EU"
        residence_relative = any(phrase in normalized for phrase in (
            "country you are based in", "country you re based in",
            "country where you are based", "country where you currently reside",
            "country you currently reside in", "your country of residence",
            "location you currently reside", "location where you currently reside",
        ))
        if residence_relative:
            region = _authorization_region_from_residence(profile)
        elif region is None:
            region = _authorization_region_from_locations(job_locations)
        record = (profile.get("work_authorization") or {}).get(region or "")
        if isinstance(record, dict) and isinstance(
            record.get("requires_sponsorship"), bool
        ):
            grounded = "Yes" if record["requires_sponsorship"] else "No"
            return grounded, f"grounded {region} sponsorship answer from profile"
    if any(phrase in normalized for phrase in (
        "name pronunciation", "name pronounciation", "pronunciation of your name",
        "pronounce your name", "phonetic spelling",
    )):
        pronunciation = str(profile.get("name_pronunciation") or "").strip()
        if not pronunciation:
            raise ValueError(
                "name pronunciation is not in the structured profile; send "
                "this application to review instead of inventing one"
            )
        return pronunciation, "grounded name pronunciation from profile"

    available_now = str(profile.get("earliest_start_date", "")).casefold() in {
        "now", "immediately", "available now",
    }
    grad_start = graduation_start_date(profile)
    if (grad_start or available_now) and any(phrase in normalized for phrase in (
        "when can you start", "available to start", "availability date",
        "when are you available", "when would you be available",
        "employment start date", "job start date", "earliest possible start date",
        "date available", "date of availability", "target start date", "desired start date",
        "preferred start date", "anticipated start date", "expected start date",
    )) and not any(phrase in normalized for phrase in (
        "graduation", "end date", "previous", "last role", "current role",
    )):
        # Still in school: every start date anchors on graduation, not today.
        grounded = (grad_start or datetime.now().date()).isoformat()
        return grounded, f"grounded start date from profile: {grounded}"
    return value, None


_CONSEQUENTIAL_TEXT = re.compile(
    r"\b(?:criminal|convict\w*|felon\w*|offen[cs]es?|cautions?|arrest\w*|misdemeano\w*|"
    r"sanction\w*|debarr\w*|authori[sz]ed to work|authori[sz]ation to work|sponsor\w*|visas?|"
    r"citizen\w*|permanent resident|security clearance|export control\w*|gender|race|"
    r"ethnic\w*|hispanic|latin[oax]|veteran|disabilit\w*|non ?compet\w*|non ?solicit\w*)\b",
    re.I)


def _consequential_text_guard(
    loc: Any, value: str, profile: dict[str, Any] | None, job_locations: list[str] | None,
) -> str | None:
    """Typed answers to legal/history/demographic questions must be profile-backed.

    Free-text inputs bypass the dropdown guards, so without this a model
    could type "No" to "Do you have any unspent convictions?" with no fact
    behind it. Allowed only when the deterministic resolver produced the
    same answer (or a standing required consent covers an affirmative).
    """
    if not profile:
        return None
    label = " ".join(str(loc.get_attribute(attr) or "") for attr in (
        "data-applyd-label", "data-applyd-question", "aria-label", "placeholder"))
    if not _CONSEQUENTIAL_TEXT.search(label):
        return None
    wanted = _normalize_option_text(value)
    if required_consent_authorized(label, profile, required=True) and re.match(r"^(?:yes|i agree|i consent)\b", wanted):
        return None
    answer = resolve_text_question(label, profile, job_locations=job_locations or (), required=True)
    expected = _normalize_option_text(str(answer.value)) if answer and answer.value is not None else ""
    if expected and (expected == wanted or (
            re.match(r"^(yes|no)\b", expected) and re.match(r"^(yes|no)\b", wanted)
            and expected.split()[0] == wanted.split()[0])):
        return None
    return ("consequential question (legal status, history, or demographics) has no "
            "matching structured profile fact; typed answer blocked; send to review")


def fill(
    page: Page, ref: str, value: str, profile: dict[str, Any] | None = None,
    resume_text: str = "", job_locations: list[str] | None = None,
) -> str:
    try:
        loc = _ref_locator(page, ref)
        if loc.get_attribute("role") == "combobox":
            return _err(
                f"fill {ref}: comboboxes require open_dropdown/pick_option or "
                "fill_autocomplete so the hidden selection is preserved"
            )
        value, grounding_note = _grounded_fill_value(
            page, ref, value, profile, resume_text, job_locations
        )
        if grounding_note is None:
            blocked = _consequential_text_guard(loc, value, profile, job_locations)
            if blocked:
                return _err(f"fill {ref}: {blocked}")
        if _should_humanize(page):
            _human_type(loc, value)
        else:
            loc.fill(value, timeout=10000)
        suffix = f"; {grounding_note}" if grounding_note else ""
        return _ok(f"filled {ref} ({len(value)} chars){suffix}")
    except _OptionalNarrativeField:
        return 'skipped: optional narrative field left unchanged'
    except Exception as e:
        return _err(f"fill {ref}: {type(e).__name__}: {e}")


def fill_many(
    page: Page, fields: list[dict[str, str]], profile: dict[str, Any] | None = None,
    resume_text: str = "", job_locations: list[str] | None = None,
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
            val, grounding_note = _grounded_fill_value(
                page, ref, val, profile, resume_text, job_locations
            )
            if grounding_note is None:
                blocked = _consequential_text_guard(loc, val, profile, job_locations)
                if blocked:
                    out.append(f"  err: {ref} :: {blocked}")
                    continue
            if _should_humanize(page):
                _human_type(loc, val)
                # Brief think-time between fields — but not after the last one.
                if i < len(fields) - 1:
                    time.sleep(_jitter(_THINK_BETWEEN_FIELDS))
            else:
                loc.fill(val, timeout=8000)
            suffix = f"; {grounding_note}" if grounding_note else ""
            out.append(f"  ok: {ref} ({len(val)} chars){suffix}")
        except _OptionalNarrativeField:
            out.append(f"  skipped: {ref} optional narrative field left unchanged")
        except Exception as e:
            out.append(f"  err: {ref} :: {type(e).__name__}: {e}")
    return f"fill_many ({len(fields)} fields):\n" + "\n".join(out)


def click_many(
    page: Page, refs: list[str], profile: dict[str, Any] | None = None,
    job_locations: list[str] | None = None,
) -> str:
    out = []
    for ref in refs:
        try:
            guard = _profile_click_guard(page, ref, profile, job_locations)
            if guard:
                out.append(f"  err: {ref} :: {guard.removeprefix('error: ')}")
                continue
            if _is_submit_control(page, ref):
                out.append(f"  err: {ref} :: submit controls must use the submit tool")
                continue
            out.append(f"  ok: {_click_with_overlay_fallback(page, ref, timeout=8000)}")
        except Exception as e:
            out.append(f"  err: {ref} :: {type(e).__name__}: {e}")
    prefix = "error: " if any(line.lstrip().startswith("err:") for line in out) else ""
    return prefix + f"click_many ({len(refs)} items):\n" + "\n".join(out)


def fill_autocomplete(page: Page, ref: str, value: str) -> str:
    """Fill a typeahead field (Lever location, Google Places, etc.) where a
    suggestion must be PICKED, not just typed — these widgets keep the real
    value in a hidden input (e.g. Lever's `selectedLocation`) and clear the
    visible field on submit if nothing was selected. Types keystroke-by-
    keystroke so suggestion listeners fire, then picks a rendered suggestion.
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
        # Mark the active control so pick_option can persist the chosen label
        # on React comboboxes whose visible <input> is cleared after selection.
        loc.evaluate(
            """el => {
                document.querySelectorAll('[data-applyd-combobox-open]')
                    .forEach(e => e.removeAttribute('data-applyd-combobox-open'));
                el.setAttribute('data-applyd-combobox-open', '1');
            }"""
        )
        loc.click(timeout=5000)
        loc.fill("", timeout=5000)
        loc.press_sequentially(value, delay=80, timeout=20000)
        page.wait_for_timeout(1500)  # let async suggestions render
        options = page.evaluate(_OPTIONS_JS)
        if options:
            selected = pick_option(page, options[0]["ref"])
            if selected.startswith("error:"):
                return selected.replace("pick_option", "fill_autocomplete", 1)
            picked = repr(options[0]["text"])
        else:
            return _err(
                f"fill_autocomplete {ref}: no selectable suggestion rendered; "
                "typed text is not a confirmed selection. Inspect the dropdown "
                "or send to review; do not plain-fill or press Enter blindly."
            )
        page.wait_for_timeout(500)
        final = (loc.evaluate(
            "el => el.value || el.getAttribute('data-applyd-picked') || ''"
        ) or "").strip()
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
        # A failed selection can leave a portal menu covering the next field.
        # Close only a different owner; opening the same control is idempotent.
        other_open = loc.evaluate("""el => Array.from(
            document.querySelectorAll('[data-applyd-combobox-open="1"]')
        ).some(other => other !== el)""")
        if other_open is True:
            page.keyboard.press("Escape")
            page.evaluate("""() => {
                document.querySelectorAll('[data-applyd-combobox-open]')
                    .forEach(e => e.removeAttribute('data-applyd-combobox-open'));
                document.querySelectorAll('[data-applyd-ref^="o"]')
                    .forEach(e => e.removeAttribute('data-applyd-ref'));
            }""")
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

        # A prior select_option/open_dropdown may have deliberately left this
        # combobox open after returning unmatched real choices. Clicking the
        # trigger again closes React/Greenhouse lists, which caused the model
        # to burn several turns trying fill/open/click fallbacks. Treat open as
        # idempotent while this control still owns a visible option list.
        if loc.get_attribute("data-applyd-combobox-open") == "1":
            options = page.evaluate(_OPTIONS_JS)
            if options:
                lines = "\n".join(f"  {o['ref']}: {o['text']}" for o in options)
                return f"opened {ref} (already open), {len(options)} options:\n{lines}"

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


def inspect_dropdowns(page: Page, refs: list[str]) -> str:
    """Read several dropdown catalogs without leaving one open.

    Option refs cannot survive switching between custom React dropdowns, so
    this tool intentionally returns labels only. The next model turn should
    batch select_option(ref, exact_label) calls using those real choices.
    """
    results: list[str] = []
    for ref in refs[:20]:
        opened = open_dropdown(page, ref)
        if opened.startswith("error:"):
            results.append(f"  {ref}: {opened}")
            continue
        options = _read_options(page)
        labels = [str(option.get("text") or "")[:120] for option in options[:80]]
        results.append(f"  {ref}: {labels!r}")
        try:
            loc = _ref_locator(page, ref)
            tag = loc.evaluate("el => el.tagName.toLowerCase()", timeout=2000)
            if tag != "select":
                page.keyboard.press("Escape")
            loc.evaluate(
                "el => el.removeAttribute('data-applyd-combobox-open')",
                timeout=2000,
            )
            page.evaluate(
                """() => document.querySelectorAll('[data-applyd-ref^="o"]')
                    .forEach(el => el.removeAttribute('data-applyd-ref'))"""
            )
        except Exception:
            # Inspection already succeeded; stale option refs are harmless and
            # the next open/select call remints them before interaction.
            pass
    return f"inspected {len(results)} dropdowns:\n" + "\n".join(results)


def pick_option(
    page: Page,
    option_ref: str,
    profile: dict[str, Any] | None = None,
    company: str = "",
    job_locations: list[str] | None = None,
) -> str:
    """Click an option previously surfaced by open_dropdown."""
    try:
        loc = _ref_locator(page, option_ref)
        tag = loc.evaluate("el => el.tagName.toLowerCase()", timeout=2000)
        option_text = (
            loc.inner_text(timeout=2000) or loc.get_attribute("value") or ""
        ).strip()[:80]
        target = page.evaluate(
            """() => {
                const el = document.querySelector('[data-applyd-combobox-open]');
                return el ? {
                    ref: el.getAttribute('data-applyd-ref') || '',
                    label: el.getAttribute('data-applyd-label') || ''
                } : {ref: '', label: ''};
            }"""
        )
        if isinstance(target, dict):
            target_ref = str(target.get("ref") or "")
            target_label = str(target.get("label") or "")
        else:
            # Preserve compatibility with simple test doubles and older traces.
            target_ref = ""
            target_label = str(target or "")
        if target_ref:
            profile_guard = _select_profile_guard(
                page, target_ref, option_text, profile, "", job_locations, company
            )
            if profile_guard:
                return profile_guard.replace(
                    f"select_option {target_ref}", f"pick_option {option_ref}", 1
                )
        offered = _option_texts(_read_options(page)) if profile else []
        date_guard = _date_profile_guard(target_label, option_text, profile, offered=offered)
        if date_guard:
            return date_guard.replace(
                "error: ", f"error: pick_option {option_ref}: ", 1
            )
        if target_ref and profile and _nearest_offered_ok(target_label, option_text, profile, offered):
            _mark_nearest_ok(page, target_ref, option_text)
        event_guard = _recruitment_event_profile_guard(
            target_label, option_text, profile, company,
            error_prefix=f"pick_option {option_ref}",
        )
        if event_guard:
            return event_guard
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


def _historical_production_claim(question: str) -> bool:
    """Spot binary questions asserting specific past production/business work.

    A related keyword in a resume does not prove the entire compound claim.
    For instance, a shipped recommendation feature does not establish work on
    advertising revenue, real production traffic, or conversion metrics.
    """
    text = _normalize_option_text(question)
    return bool(
        re.search(r"\b(?:have you|did you|do you have)\b", text)
        and re.search(r"\b(?:built|deployed|shipped|worked on|experience with)\b", text)
        and any(term in text for term in (
            "production", "advertis", "ranking", "feedback signals",
            "business metrics", "revenue", "conversion", "traffic",
            "optimizes decisions",
        ))
    )


def _historical_application_answer(profile: dict[str, Any], question: str) -> bool | None:
    answers = profile.get("historical_application_answers") or {}
    if not isinstance(answers, dict):
        return None
    normalized = _normalize_option_text(question)
    for saved_question, answer in answers.items():
        if _normalize_option_text(str(saved_question)) == normalized and isinstance(answer, bool):
            return answer
    return None


def _recruitment_event_profile_guard(
    label: str,
    value: str,
    profile: dict[str, Any] | None,
    company: str,
    *,
    error_prefix: str,
) -> str | None:
    """Ground target-company recruitment-event answers, including raw picks."""
    question = _normalize_option_text(label)
    if not any(phrase in question for phrase in (
        "recruitment event", "event you attended", "which event",
    )):
        return None
    # "Receive recruitment marketing communications ... recruitment events"
    # is an opt-in consent, not a question about events attended.
    if re.search(r"\b(?:receive|communications?|marketing|opt in|subscribe|updates)\b", question):
        return None
    background = (profile or {}).get("background_defaults") or {}
    recruitment_events = background.get("recruitment_events") or {}
    company_events = next((
        events for event_company, events in recruitment_events.items()
        if str(event_company).casefold() == company.casefold()
    ), None) if isinstance(recruitment_events, dict) and company else None
    if not isinstance(company_events, list):
        return _err(
            f"{error_prefix}: target-company recruitment-event history is not "
            "in the structured profile; send to review"
        )

    desired = _normalize_option_text(value)
    says_yes = desired in {"yes", "true"}
    says_no = desired in {"no", "false"}
    if (says_yes or says_no) and says_yes != bool(company_events):
        return _err(
            f"{error_prefix}: refused recruitment-event answer {value!r}; "
            f"structured profile records {len(company_events)} attended event(s)"
        )
    if not company_events and not (says_no or desired in {
        "n a", "na", "not applicable", "none",
    }):
        return _err(
            f"{error_prefix}: refused invented recruitment event {value!r}; "
            "structured profile records none attended"
        )
    if company_events and not (says_yes or any(
        _normalize_option_text(str(event)) == desired for event in company_events
    )):
        return _err(
            f"{error_prefix}: refused unrecorded recruitment event {value!r}"
        )
    return None


_MONTH_NUMBERS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _month_year(value: str) -> tuple[int, int | None] | None:
    """Parse conservative year/month dropdown labels without guessing seasons."""
    text = _normalize_option_text(value)
    iso = re.fullmatch(r"(20\d{2})(?:\s+(0?[1-9]|1[0-2]))?", text)
    if iso:
        return int(iso.group(1)), int(iso.group(2)) if iso.group(2) else None
    match = re.fullmatch(r"([a-z]+)\s+(20\d{2})", text)
    if match and match.group(1) in _MONTH_NUMBERS:
        return int(match.group(2)), _MONTH_NUMBERS[match.group(1)]
    return None


def _month_year_range(value: str) -> tuple[int, int, int] | None:
    """Parse an ATS graduation window into ``(year, first_month, last_month)``.

    Job forms commonly group graduation cohorts as ``Spring 2027`` or
    ``April to July 2027``. These are bounded factual ranges, not guesses: an
    exact structured graduation month either falls inside the window or it
    does not.
    """
    text = _normalize_option_text(value)
    season = re.fullmatch(r"(spring|summer|fall|autumn)\s+(20\d{2})", text)
    if season:
        bounds = {
            "spring": (3, 5),
            "summer": (6, 8),
            "fall": (9, 11),
            "autumn": (9, 11),
        }[season.group(1)]
        return int(season.group(2)), bounds[0], bounds[1]

    months = re.findall(
        r"\b(" + "|".join(sorted(_MONTH_NUMBERS, key=len, reverse=True)) + r")\b",
        text,
    )
    years = re.findall(r"\b(20\d{2})\b", text)
    if len(months) == 2 and len(set(years)) == 1:
        first, last = (_MONTH_NUMBERS[month] for month in months)
        if first <= last:
            return int(years[0]), first, last
    return None


def _question_date(value: str) -> tuple[int, int, int | None] | None:
    """Parse an explicit day/month/year embedded in a question label."""
    text = _normalize_option_text(value)
    months = "|".join(sorted(_MONTH_NUMBERS, key=len, reverse=True))
    match = re.search(
        rf"\b(?:(\d{{1,2}})(?:st|nd|rd|th)?\s+)?({months})\s+(20\d{{2}})\b",
        text,
    )
    if not match:
        return None
    day = int(match.group(1)) if match.group(1) else None
    return int(match.group(3)), _MONTH_NUMBERS[match.group(2)], day


_GRADUATION_FIELD_PHRASES = (
    "graduation date", "expected graduation", "graduation month", "graduation year",
)
_START_FIELD_PHRASES = (
    "target start date", "employment start date", "job start date",
    "available to start", "availability date",
)


def graduation_nearest_policy(profile: dict[str, Any] | None) -> bool:
    """User opt-in: graduation dropdowns may take the closest offered date."""
    policy = (profile or {}).get("application_policy") or {}
    return isinstance(policy, dict) and policy.get("graduation_date_nearest_option") is True


def _nearest_offered_ok(
    label: str, value: str, profile: dict[str, Any], offered: Sequence[str] | None,
    today: date | None = None,
) -> bool:
    """True when ``value`` is exactly the offered option nearest the anchor.

    Start dates anchor on graduation (a preference). Graduation dates take
    this route only under the explicit user policy above.
    """
    if not offered:
        return False
    field = _normalize_option_text(label)
    if any(phrase in field for phrase in _GRADUATION_FIELD_PHRASES):
        if not graduation_nearest_policy(profile):
            return False
        anchor = str(profile.get("expected_grad_date") or "")
    elif any(phrase in field for phrase in _START_FIELD_PHRASES):
        start = graduation_start_date(profile, today=today)
        # Relative windows ("More than 60 days after accepting"): allowed only
        # when it is the single window containing the days until the start.
        from .deterministic_questions import _relative_window
        windows = {text: _relative_window(text) for text in offered}
        windows = {text: w for text, w in windows.items() if w}
        if len(windows) >= 2 and _relative_window(value):
            current = today or date.today()
            days = max(0, ((start or current) - current).days)
            fits = [text for text, (lo, hi) in windows.items() if lo <= days <= hi]
            return len(fits) == 1 and _normalize_option_text(fits[0]) == _normalize_option_text(value)
        anchor = (start or today or date.today()).strftime("%Y-%m")
    else:
        return False
    labels = {str(i): str(text) for i, text in enumerate(offered)}
    graduation = any(phrase in field for phrase in _GRADUATION_FIELD_PHRASES)
    key = option_semantics.bind_nearest_month(labels, anchor, prefer_after=not graduation)
    return key is not None and (
        _normalize_option_text(labels[key]) == _normalize_option_text(value))


def _offered_option_texts(page: Page, ref: str) -> list[str]:
    """Options the form offers for ``ref``: native <select> or open listbox."""
    try:
        native = _ref_locator(page, ref).evaluate(
            "el => el.tagName === 'SELECT' ? [...el.options].map(o => (o.text || '').trim()) : null",
            timeout=3000,
        )
        if isinstance(native, list) and native:
            return [str(text) for text in native]
        return _option_texts(_read_options(page))
    except Exception:  # noqa: BLE001
        return []


def _option_texts(options: Any) -> list[str]:
    """Option labels from read_options results (dicts or plain strings)."""
    if not isinstance(options, list):
        return []
    return [str(o.get("text", "")) if isinstance(o, dict) else str(o) for o in options]


def _mark_nearest_ok(page: Page, ref: str, value: str) -> None:
    """Record that the runner verified this nearest-date choice (read at submit)."""
    try:
        _ref_locator(page, ref).evaluate(
            "(el, v) => el.setAttribute('data-applyd-nearest-ok', v)", value, timeout=3000)
    except Exception:  # noqa: BLE001
        pass


def _date_profile_guard(
    label: str,
    value: str,
    profile: dict[str, Any] | None,
    *,
    today: date | None = None,
    offered: Sequence[str] | None = None,
) -> str | None:
    """Reject dropdown dates that contradict grounded education/availability."""
    if not profile:
        return None
    if _nearest_offered_ok(label, value, profile, offered, today=today):
        return None
    field = _normalize_option_text(label)
    chosen = _normalize_option_text(value)
    selected = _month_year(value)
    expected_raw = str(profile.get("expected_grad_date") or "").strip()
    expected = _month_year(expected_raw)

    season_question = re.search(
        r"\bgraduat\w*\s+(spring|summer|fall|autumn|winter)(?:\s+of)?\s+(20\d{2})\b",
        field,
    )
    if season_question and expected and chosen in {"yes", "true", "no", "false"}:
        season = season_question.group(1)
        season_months = {
            "spring": {3, 4, 5},
            "summer": {6, 7, 8},
            "fall": {9, 10, 11},
            "autumn": {9, 10, 11},
            "winter": {12, 1, 2},
        }
        matches = (
            expected[0] == int(season_question.group(2))
            and expected[1] in season_months[season]
        )
        says_yes = chosen in {"yes", "true"}
        if says_yes != matches:
            return _err(
                f"date selection blocked: {value!r} contradicts grounded "
                f"graduation date {expected_raw!r} for {season} "
                f"{season_question.group(2)}"
            )

    if any(phrase in field for phrase in (
        "graduation date", "expected graduation", "graduation month",
        "graduation year",
    )):
        if re.fullmatch(r'20\d{2}-\d{2}', expected_raw) and re.search(
                r'\b(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]20\d{2}|[a-z]+\s+\d{1,2},?\s+20\d{2}|\d{1,2}\s+[a-z]+\s+20\d{2})\b',
                value.casefold()):
            return _err('date selection blocked: exact graduation day is not grounded in the month-only profile')
        if not expected:
            return _err(
                "date selection blocked: expected graduation date is not in "
                "the structured profile"
            )
        selected_range = _month_year_range(value)
        # Separate month controls contain no year. Only accept this shape for
        # an explicitly month-labeled field, against the authoritative date.
        if "graduation month" in field and chosen in _MONTH_NUMBERS:
            if _MONTH_NUMBERS[chosen] == expected[1]:
                return None
            return _err(
                f"date selection blocked: {value!r} contradicts grounded "
                f"graduation date {expected_raw!r}"
            )
        if not selected and not selected_range:
            return _err(
                f"date selection blocked: cannot prove {value!r} matches "
                f"grounded graduation date {expected_raw!r}"
            )
        if selected_range:
            matches = (
                selected_range[0] == expected[0]
                and expected[1] is not None
                and selected_range[1] <= expected[1] <= selected_range[2]
            )
        else:
            matches = bool(
                selected
                and selected[0] == expected[0]
                and (selected[1] is None or selected[1] == expected[1])
            )
        if not matches:
            return _err(
                f"date selection blocked: {value!r} contradicts grounded "
                f"graduation date {expected_raw!r}"
            )

    if any(phrase in field for phrase in (
        "target start date", "employment start date", "job start date",
        "available to start", "availability date",
    )):
        earliest_raw = str(profile.get("earliest_start_date") or "").strip()
        boolean_choice = chosen in {"yes", "true", "no", "false"}
        question_date = _question_date(label)
        if boolean_choice and question_date:
            question_year, question_month, question_day = question_date
            current = today or date.today()
            if earliest_raw.casefold() in {"now", "immediately", "available now"}:
                if question_day is None:
                    available = (question_year, question_month) >= (
                        current.year, current.month
                    )
                else:
                    available = date(
                        question_year, question_month, question_day
                    ) >= current
            else:
                earliest = _month_year(earliest_raw)
                if not earliest:
                    return _err(
                        "date selection blocked: earliest start date is not in "
                        "the structured profile"
                    )
                available = (question_year, question_month) >= (
                    earliest[0], earliest[1] or 1
                )
            says_yes = chosen in {"yes", "true"}
            if says_yes != available:
                return _err(
                    f"date selection blocked: {value!r} contradicts grounded "
                    f"availability {earliest_raw!r} for the date in {label!r}"
                )
            return None
        if earliest_raw.casefold() in {"now", "immediately", "available now"}:
            if chosen in {
                "now", "immediately", "available now", "as soon as possible",
            }:
                return None
            if not selected:
                return _err(
                    f"date selection blocked: cannot prove {value!r} is a "
                    "valid future start date"
                )
            current = today or date.today()
            selected_month = selected[1] or 1
            if (selected[0], selected_month) < (current.year, current.month):
                return _err(
                    f"date selection blocked: {value!r} is before the current "
                    f"month {current:%Y-%m}"
                )
        else:
            earliest = _month_year(earliest_raw)
            if earliest and selected:
                earliest_month = earliest[1] or 1
                selected_month = selected[1] or 1
                if (selected[0], selected_month) < (earliest[0], earliest_month):
                    return _err(
                        f"date selection blocked: {value!r} is before grounded "
                        f"earliest start date {earliest_raw!r}"
                    )
    return None


def _option_semantic(value: str) -> str | None:
    """Map common ATS wording variants to a conservative shared meaning."""
    text = _normalize_option_text(value)
    if re.match(r"^(?:yes|true)\b", text):
        return "boolean:yes"
    if re.match(r"^i (?:agree|consent|accept|acknowledge)\b", text):
        return "boolean:yes"
    if re.match(r"^(?:no|false)\b", text):
        return "boolean:no"
    if re.search(r"\bbachelor(?:s)?\b", text):
        return "degree:bachelor"
    if re.search(r"\bmaster(?:s)?\b", text):
        return "degree:master"
    if re.search(r"\bassociate(?:s)?\b", text):
        return "degree:associate"
    if re.search(r"\b(phd|doctor of philosophy)\b", text):
        return "degree:phd"
    if (
        any(phrase in text for phrase in ("company career", "company careers"))
        and any(term in text for term in ("page", "site", "website"))
    ) or "career website" in text or re.search(
        r"\bcareers?\s+(?:page|site|website)\b", text
    ):
        return "source:company-careers"
    if any(phrase in text for phrase in (
        "prefer not to say", "decline to self identify", "decline to answer",
        "do not wish to answer", "don t wish to answer",
    )):
        return "neutral:decline"
    if text in {
        "black", "black or african american", "black or of african descent",
    }:
        return "demographic:black-african-descent"
    return None


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
        # Whole-word containment only: "no" must not match inside "not", and
        # "Engineering (Non-Computer Science)" is not "Computer Science".
        if text and f" non {wanted} " not in f" {text} " and (
                f" {wanted} " in f" {text} " or f" {text} " in f" {wanted} "):
            compatible.append(option)
    if len(compatible) == 1:
        return compatible[0]
    semantic = _option_semantic(desired)
    if semantic:
        equivalent = [
            option for option in options
            if _option_semantic(option.get("text", "")) == semantic
        ]
        if len(equivalent) == 1:
            return equivalent[0]
    # Same meaning, different wording: dates as months ("2027-04" ↔ "April
    # 2027"), veteran/disability phrasing, sentence-form yes/no options, and
    # "Not in the U.S." for a Canadian province. Still unique-or-nothing.
    labels = option_semantics.labels_of(options)
    key = (option_semantics.bind_same_month(labels, desired)
           or option_semantics.bind_semantic(labels, desired)
           or option_semantics.bind_outside_option(labels, desired))
    return options[int(key)] if key is not None else None


def _field_specific_option_value(field_label: str, desired: str) -> str:
    """Translate a grounded value into an ATS-specific explicit option label."""
    label = _normalize_option_text(field_label)
    value = _normalize_option_text(desired)
    if (
        "held a u s security clearance in the past" in label
        and value in {"no", "false"}
    ):
        return "N/A - have never held U.S. security clearance"
    return desired


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
    company: str = "",
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
    question = " ".join(
        re.sub(
            r"[^a-z0-9]+", " ",
            str(loc.get_attribute("data-applyd-question") or label).casefold(),
        ).split()
    )
    degree_error = _degree_claim_guard(question, value, profile)
    if degree_error:
        return degree_error
    recording_consent: bool | None = None
    required = (loc.get_attribute("aria-required") == "true" or loc.get_attribute("required") is not None
                or loc.get_attribute("data-applyd-required") == "true")
    standing_consent = required_consent_authorized(question, profile, required=required)
    if (
        any(term in question for term in ("recording", "recorded", "record interview"))
        and any(term in question for term in ("consent", "agree", "permission"))
    ):
        recording_consent = (profile.get("application_policy") or {}).get(
            "interview_recording_consent"
        )
        if standing_consent:
            recording_consent = True
        if not isinstance(recording_consent, bool):
            return _err(
                f"select_option {ref}: interview-recording consent is not in "
                "the structured profile; send to review"
            )
    desired = _normalize_option_text(value)
    if immigration_authorization_required_question(question) and not re.search(r'\bsponsor', question):
        answer = resolve_question(question, profile, {'yes': 'Yes', 'no': 'No'},
                                  job_locations=job_locations or ())
        actual = True if re.match(r'^(?:yes|true)\b', desired) else False if re.match(r'^(?:no|false)\b', desired) else None
        if not isinstance(answer.value, bool) or actual is not answer.value:
            return _err('visa/authorization requirement is not the same as current authorization; required fact not in the structured profile or semantically unsupported; unsupported answer blocked')
        return None
    if isinstance(recording_consent, bool):
        says_yes = desired in {"yes", "true"} or desired.startswith("yes ")
        says_no = desired in {"no", "false"} or desired.startswith("no ")
        if (says_yes and not recording_consent) or (says_no and recording_consent):
            return _err(
                f"select_option {ref}: refused contradictory "
                "interview-recording consent"
            )
    if (not standing_consent and re.search(r'\b(?:arbitration|non[ -]?compete|waiv(?:e|er) of rights)\b', question + ' ' + label)
            and not _covenant_answer_ok(question + ' ' + label, value, profile)):
        return _err(f'select_option {ref}: unusual legal attestation requires human review')

    if _historical_production_claim(question):
        recorded = _historical_application_answer(profile, question)
        if not isinstance(recorded, bool):
            return _err(
                f"select_option {ref}: production/business-history claim is not "
                "explicitly attested in historical_application_answers; send "
                "this application to review"
            )
        says_yes = desired in {"yes", "true"} or desired.startswith("yes ")
        says_no = desired in {"no", "false"} or desired.startswith("no ")
        if (says_yes and not recorded) or (says_no and recorded):
            return _err(
                f"select_option {ref}: refused contradictory production/business-"
                f"history answer {value!r}"
            )

    # Some location dropdowns bundle a neutral "none of these offices" choice
    # with an additional exclusive preference claim.  Stripe, for example,
    # offered "None of the above - I am only looking for a remote role".  The
    # model selected it even though the profile explicitly allows onsite and
    # hybrid work.  Do not let the absence of a truthful office option turn
    # into a fabricated remote-only preference.
    preferences = profile.get("employment_preferences") or {}
    claims_remote_only = bool(
        re.search(r"\b(?:only\s+(?:looking|interested)\s+for\s+(?:a\s+)?remote|remote\s+only)\b", desired)
    )
    open_to_non_remote = any(
        preferences.get(key) is True
        for key in (
            "willing_to_work_onsite",
            "willing_to_work_hybrid",
            "willing_to_work_any_onsite_schedule",
        )
    )
    if claims_remote_only and open_to_non_remote:
        return _err(
            f"select_option {ref}: refused false remote-only preference "
            f"{value!r}; structured employment preferences allow onsite "
            "or hybrid work. Choose a truthful non-exclusive option or send "
            "to review."
        )

    language_proficiency_question = (
        any(term in question for term in (
            "language", "english", "french", "spanish", "german",
            "portuguese", "mandarin", "cantonese",
        ))
        and any(term in question for term in (
            "proficiency", "fluency", "fluent", "conversational", "cefr",
        ))
    )
    if language_proficiency_question:
        spoken_languages = [
            _normalize_option_text(str(language))
            for language in (profile.get("spoken_languages") or [])
            if str(language).strip()
        ]
        proficiency = profile.get("language_proficiency")
        if not isinstance(proficiency, dict) or not proficiency:
            return _err(
                f"select_option {ref}: language proficiency is not in the "
                "structured profile; spoken_languages alone does not ground "
                "native, bilingual, CEFR, or fluency claims. Send to review."
            )
        matching_values = [
            str(level).strip()
            for language, level in proficiency.items()
            if _normalize_option_text(str(language)) in question
            and str(level or "").strip()
        ]
        if not matching_values and len(proficiency) == 1:
            matching_values = [
                str(level).strip() for level in proficiency.values()
                if str(level or "").strip()
            ]
        if not matching_values:
            return _err(
                f"select_option {ref}: no explicit proficiency is recorded "
                "for the language in this question; send to review"
            )
        if "bilingual" in desired and len(spoken_languages) < 2:
            return _err(
                f"select_option {ref}: refused false bilingual claim {value!r}; "
                "structured spoken_languages contains fewer than two languages"
            )
        recorded = {_normalize_option_text(level) for level in matching_values}
        compatible = any(
            desired == level or level in desired or desired in level
            for level in recorded
        )
        if not compatible:
            return _err(
                f"select_option {ref}: refused ungrounded language proficiency "
                f"{value!r}; use the explicit language_proficiency value or "
                "send to review"
            )

    if "high school" in question and any(term in question for term in (
        "perform", "performance", "rank", "ranking", "score",
    )):
        performance = profile.get("high_school_performance")
        subject = (
            "mathematics" if any(term in question for term in ("math", "mathematics"))
            else "native_language" if "native language" in question
            else "evidence"
        )
        grounded = performance.get(subject) if isinstance(performance, dict) else None
        grounded_normalized = _normalize_option_text(str(grounded or ""))
        if not grounded_normalized:
            return _err(
                f"select_option {ref}: high-school academic performance is "
                "not in the structured profile; send to review"
            )
        if desired != grounded_normalized:
            return _err(
                f"select_option {ref}: refused unsupported high-school "
                f"performance {value!r}; use the structured profile value"
            )

    years_question = bool(
        re.search(r"\bhow many years\b", question)
        and "experience" in question
    )
    if years_question:
        known_years: float | int | None = None
        source_name = ""
        if "professional" in question:
            candidate = profile.get("years_professional_experience")
            if isinstance(candidate, (int, float)):
                known_years = candidate
                source_name = "years_professional_experience"
        else:
            technology_years = profile.get("technology_experience_years") or {}
            if isinstance(technology_years, dict):
                for technology, candidate in technology_years.items():
                    normalized_technology = _normalize_option_text(str(technology))
                    if (
                        normalized_technology
                        and normalized_technology in question
                        and isinstance(candidate, (int, float))
                    ):
                        known_years = candidate
                        source_name = f"technology_experience_years.{technology}"
                        break
        if known_years is None:
            return _err(
                f"select_option {ref}: experience duration is not grounded in "
                "the structured profile; send to review instead of selecting "
                f"{value!r}"
            )
        numbers = [float(number) for number in re.findall(r"\d+(?:\.\d+)?", desired)]
        matches = True
        if len(numbers) >= 2:
            matches = numbers[0] <= known_years <= numbers[1]
        elif numbers:
            boundary = numbers[0]
            if any(token in desired for token in ("less than", "under", "below")):
                matches = known_years < boundary
            elif any(token in desired for token in ("more than", "over", "above", "+")):
                matches = known_years >= boundary
            else:
                matches = known_years == boundary
        if not matches:
            return _err(
                f"select_option {ref}: refused experience range {value!r}; "
                f"structured {source_name}={known_years}"
            )

    degree_focus_question = any(phrase in question for phrase in (
        "does your degree focus", "is your degree focused",
        "degree concentration", "degree specialization",
    ))
    if degree_focus_question:
        raw_focus_areas = profile.get("degree_focus_areas")
        if not isinstance(raw_focus_areas, list):
            return _err(
                f"select_option {ref}: degree focus is not in the structured "
                "profile; send to review"
            )
        focus_areas = {
            _normalize_option_text(str(area))
            for area in raw_focus_areas
            if str(area).strip()
        }
        matches_focus = any(area in question for area in focus_areas)
        says_no = desired in {"no", "false"} or desired.startswith("no ")
        says_yes = desired in {"yes", "true"} or desired.startswith("yes ")
        if (says_yes and not matches_focus) or (says_no and matches_focus):
            return _err(
                f"select_option {ref}: refused degree-focus answer {value!r}; "
                "it contradicts structured degree_focus_areas"
            )

    generic_recruiting_consent_question = (
        any(phrase in question for phrase in (
            "recruitment marketing", "recruiting marketing", "talent network",
            "future job opportunities", "training opportunities", "job alerts",
        ))
        and any(term in question for term in (
            "communication", "contact me", "receive information", "receive updates",
        ))
    )
    communication_consent_question = (
        any(term in question for term in ("sms", "whatsapp", "text message"))
        and any(term in question for term in ("communication", "message", "contact"))
    )
    if generic_recruiting_consent_question or communication_consent_question:
        policy = profile.get("application_policy") or {}
        policy_key = (
            "recruiting_communications_consent"
            if generic_recruiting_consent_question
            else "sms_recruiting_consent"
        )
        consent = policy.get(policy_key)
        if standing_consent:
            consent = True
        if not isinstance(consent, bool):
            return _err(
                f"select_option {ref}: recruiting communications consent is not "
                "in the structured profile; send to review"
            )
        says_no = desired in {"no", "false"} or desired.startswith("no ")
        says_yes = desired in {"yes", "true"} or desired.startswith("yes ")
        if (says_yes and not consent) or (says_no and consent):
            return _err(
                f"select_option {ref}: refused contradictory recruiting "
                f"communications answer {value!r}"
            )

    masters_interest_question = (
        "master" in question
        and "degree" in question
        and (
            any(term in question for term in ("interested", "interest in"))
            or (
                "full time employment" in question
                and any(term in desired for term in ("interested", "interest in"))
            )
        )
    )
    if masters_interest_question:
        preferences = profile.get("employment_preferences") or {}
        willing = preferences.get("willing_to_pursue_masters_while_working")
        if not isinstance(willing, bool):
            return _err(
                f"select_option {ref}: interest in pursuing a Master's degree "
                "while working is not in the structured profile; send to review"
            )
        says_no = desired in {"no", "false"} or desired.startswith("no ")
        says_yes = desired in {"yes", "true"} or desired.startswith("yes ")
        if says_yes != willing and (says_yes or says_no):
            return _err(
                f"select_option {ref}: refused contradictory Master's-interest "
                f"answer {value!r}"
            )

    # Only compare an option directly to the profile's degree when the control
    # is actually asking for a degree level/type. The previous ``"degree" in
    # question`` check also caught boolean statements such as "Are you pursuing
    # a degree? — Yes" and preference questions such as "interested in gaining
    # a Master's degree? — No", then rejected Yes/No as if they were credential
    # names. That false-positive loop caused several live ATS reviews.
    degree_question = (
        question in {"degree", "degree type", "education level"}
        or any(phrase in question for phrase in (
            "which degree", "what degree", "select your degree",
            "highest degree", "highest level of education",
            "level of education", "type of degree",
        ))
    ) and not any(phrase in question for phrase in (
        "field of study", "discipline", "major",
    ))
    profile_degree = _normalize_option_text(str(profile.get("degree") or ""))
    if degree_question and profile_degree:
        generic_bachelor = desired in {
            "bachelor", "bachelors", "bachelor s", "bachelor degree",
            "bachelors degree", "bachelor s degree", "undergraduate",
            "undergraduate degree",
        }
        generic_master = desired in {
            "master", "masters", "master s", "master degree",
            "masters degree", "master s degree", "graduate degree",
        }
        compatible = (
            desired == profile_degree
            or desired in profile_degree
            or profile_degree in desired
            or (generic_bachelor and profile_degree.startswith("bachelor"))
            or (generic_master and profile_degree.startswith("master"))
        )
        if not compatible:
            return _err(
                f"select_option {ref}: refused credential substitution {value!r}; "
                f"structured profile degree is {profile.get('degree')!r}"
            )

    event_guard = _recruitment_event_profile_guard(
        question, value, profile, company, error_prefix=f"select_option {ref}"
    )
    if event_guard:
        return event_guard

    offered = _offered_option_texts(page, ref)
    date_guard = _date_profile_guard(label, value, profile, offered=offered)
    if date_guard:
        return date_guard.replace(
            "error: ", f"error: select_option {ref}: ", 1
        )
    if _nearest_offered_ok(label, value, profile, offered):
        # The value written may be the rendered option text; tag both forms.
        nearest = next((text for text in offered
                        if _normalize_option_text(text) == _normalize_option_text(value)), value)
        _mark_nearest_ok(page, ref, nearest)

    # Never let a conditional U.S.-state control fabricate residence. Some
    # Greenhouse forms leave the field required even after Canada is selected;
    # Kimi once picked Idaho simply to satisfy it. A foreign resident may use
    # an explicit N/A option, otherwise the job must remain in review.
    region_question = any(term in label for term in (
        "which us state", "which u s state", "state are you located",
        "state do you live", "state of residence", "state province",
        "province territory", "current state",
    ))
    if region_question:
        profile_region = _normalize_option_text(str(profile.get("address_region") or ""))
        country_code = str(profile.get("address_country_code") or "").upper()
        country_name = _normalize_option_text(str(profile.get("address_country") or ""))
        outside_us = country_code not in {"", "US"} or (
            country_name not in {"", "united states", "united states of america"}
        )
        neutral = any(term in desired for term in (
            "not applicable", "not in the us", "outside the us", "none"
        ))
        matches_profile = bool(profile_region) and (
            desired == profile_region
            or profile_region in desired
            or desired in profile_region
        )
        if outside_us and "state" in label and not neutral and not matches_profile:
            return _err(
                f"select_option {ref}: refused fabricated U.S. state {value!r}; "
                f"profile residence is {profile.get('address_region')!r}, "
                f"{profile.get('address_country')!r}. Choose an explicit Not "
                "Applicable/Not in the US option or send to review."
            )
        if profile_region and not matches_profile and not neutral:
            return _err(
                f"select_option {ref}: refused location {value!r}; structured "
                f"profile region is {profile.get('address_region')!r}"
            )

    citizenship_region = None
    padded_question = f" {question} "
    if "citizen" in question:
        if any(term in padded_question for term in (
            " us ", " u s ", " united states ", " american ",
        )):
            citizenship_region = "US"
        elif "canadian" in question or " canada " in padded_question:
            citizenship_region = "CA"
        elif any(term in padded_question for term in (
            " uk ", " united kingdom ", " british ",
        )):
            citizenship_region = "UK"
    citizenships = {
        _normalize_option_text(str(item))
        for item in (profile.get("citizenships") or [])
        if str(item).strip()
    }
    if citizenship_region and citizenships:
        aliases = {
            "US": {"us", "usa", "united states", "united states of america", "american"},
            "CA": {"ca", "canada", "canadian"},
            "UK": {"uk", "gb", "united kingdom", "british"},
        }[citizenship_region]
        is_citizen = bool(citizenships & aliases)
        says_no = desired in {"no", "false"} or any(phrase in desired for phrase in (
            "not a us citizen", "not a canadian citizen", "not a british citizen",
        ))
        says_yes = desired in {"yes", "true"} or (
            "citizen" in desired and "not a" not in desired
        )
        if says_yes and not is_citizen:
            return _err(
                f"select_option {ref}: refused unsupported {citizenship_region} "
                "citizenship claim; structured citizenships do not include it"
            )
        if says_no and is_citizen:
            return _err(
                f"select_option {ref}: refused false denial of "
                f"{citizenship_region} citizenship"
            )

    legal_context = f"{question} {label}".strip()
    uk_question = (
        any(term in f" {legal_context} " for term in (
            " uk ", " united kingdom ", " britain ",
        ))
        or _authorization_region_from_locations(job_locations) == "UK"
    )
    sponsorship_question = bool(
        re.search(
            r"\b(?:require|requires|required|need|needs)\b.{0,30}\bsponsorship\b",
            question,
        )
        or any(phrase in question for phrase in (
            "sponsor you", "sponsor me", "be sponsored",
            "dependent on sponsorship",
        ))
    )
    authorization_question = not sponsorship_question and any(term in question for term in (
        "legally permitted to work", "right to work", "authorized to work", "authorised to work",
        "work authorization", "work authorisation", "work permit", "need a visa",
        "employment eligibility status",
    ))
    legal_question = authorization_question or sponsorship_question

    explicit_regions = _authorization_regions_from_text(legal_context)
    if legal_question and len(explicit_regions) > 1:
        records = profile.get("work_authorization") or {}
        required_fact = (
            "requires_sponsorship" if sponsorship_question else "authorized"
        )
        values = [
            records.get(candidate, {}).get(required_fact)
            for candidate in explicit_regions
        ]
        # A single Yes/No cannot faithfully encode conflicting country facts.
        # Do not let question order (for example "U.S. or Canada") silently
        # pick the first country and invert the applicant's truthful answer.
        if (
            all(isinstance(value, bool) for value in values)
            and len(set(values)) > 1
        ):
            return _err(
                f"select_option {ref}: mixed-jurisdiction legal question has "
                "conflicting structured facts; send to review"
            )

    # Work authorization is a consequential legal claim, so enforce it from
    # the structured profile rather than trusting the model's interpretation
    # of long ATS option labels. In particular, an option mentioning H-1B/F-1
    # can still assert that the applicant is *currently authorized*; merely
    # intending to need H-1B sponsorship does not make that statement true.
    padded_label = f" {legal_context} "
    region = explicit_regions[0] if len(explicit_regions) == 1 else None
    if any(term in padded_label for term in (
        " united states ", " u s ", " usa ", " h1b ", " h 1b ",
    )):
        region = "US"
    elif "canada" in legal_context or "canadian" in legal_context:
        region = "CA"
    elif uk_question:
        region = "UK"
    elif "european union" in legal_context or " eu " in padded_label:
        region = "EU"
    residence_relative = any(phrase in legal_context for phrase in (
        "country you are based in", "country you re based in",
        "country where you are based", "country where you currently reside",
        "country you currently reside in", "your country of residence",
        "location you currently reside", "location where you currently reside",
    ))
    if legal_question and region is None:
        selected_locations = _selected_application_locations(page)
        region = (
            _authorization_region_from_residence(profile)
            if residence_relative
            else _authorization_region_from_locations(
                selected_locations or list(job_locations or [])
            )
        )

    auth_record = (profile.get("work_authorization") or {}).get(region or "")
    # Never let an unresolved country silently bypass the legal guard. Known
    # cities are mapped explicitly above; an unknown city remains review-only.
    # The candidate's home country is used only for residence-relative wording.
    if legal_question and region is None:
        return _err(
            f"select_option {ref}: legal answer blocked: work-authorization "
            "country could not be established; send to review"
        )
    if legal_question and not isinstance(auth_record, dict):
        return _err(
            f"select_option {ref}: {region} authorization/sponsorship is not in "
            "the structured profile; send to review"
        )
    required_fact = 'requires_sponsorship' if sponsorship_question else 'authorized'
    if legal_question and not isinstance(auth_record.get(required_fact), bool):
        return _err(
            f"select_option {ref}: legal answer blocked: {region} "
            f"{required_fact} is unknown; send to review"
        )
    if legal_question and isinstance(auth_record, dict):
        denies_current_authorization = any(phrase in desired for phrase in (
            "not currently authorized", "not authorized", "not authorised",
            "do not have work authorization", "no work authorization",
            "do not have the right to work", "no right to work",
        )) or _denies_authorization(desired)
        claims_current_status = (
            "can work for" in desired
            or "authorized to work" in desired
            or "authorised to work" in desired
            or bool(re.search(
                r"\b(?:currently\s+)?hold(?:\s+a)?\s+"
                r"(?:tn|e\s*3|opt|stem\s+opt|cpt|h\s*1b)\b",
                desired,
            ))
            or bool(re.search(r"\bi am (?:a|an) [a-z ]*citizen\b", desired))
        ) and not denies_current_authorization
        if auth_record.get("authorized") is False and claims_current_status:
            return _err(
                f"select_option {ref}: refused unsupported {region} work "
                f"authorization claim {value!r}; structured profile says "
                "authorized=false"
            )

        if authorization_question:
            says_not_authorized = any(phrase in desired for phrase in (
                "not currently authorized", "not authorized", "not authorised",
                "do not have work authorization", "no work authorization",
                "do not have the right to work", "no right to work",
            )) or desired in {"no", "false"} or _denies_authorization(desired)
            says_authorized = (
                desired in {"yes", "true"}
                or any(phrase in desired for phrase in (
                    "i am authorized", "i am authorised", "authorized to work",
                    "authorised to work", "right to work",
                    "permanent work authorization", "permanent work authorisation",
                    "citizen or lawful permanent resident",
                    "green card holder", "valid work permit",
                ))
            ) and not says_not_authorized
            authorized = auth_record.get("authorized")
            if authorized is False and says_authorized:
                return _err(
                    f"select_option {ref}: refused unsupported {region} work "
                    f"authorization claim {value!r}; structured profile says "
                    "authorized=false"
                )
            if authorized is True and says_not_authorized:
                return _err(
                    f"select_option {ref}: refused false denial of {region} work "
                    "authorization; structured profile says authorized=true"
                )

        # Some ATSes place sponsorship semantics only in the option text under
        # a generic work-authorization question. Enforce those claims too.
        option_sponsorship_claim = "sponsor" in desired
        if sponsorship_question or option_sponsorship_claim:
            requires = auth_record.get("requires_sponsorship")
            says_no = bool(re.match(r"^(no|false)\b", desired)) or any(
                phrase in desired for phrase in (
                    "do not require sponsorship", "will not require sponsorship",
                    "do not need sponsorship", "will not need sponsorship", "no sponsorship",
                )
            )
            # Remove negative clauses before looking for affirmative claims.
            # 'No, I do not / will not require sponsorship' is not a Yes.
            affirmative = re.sub(
                r"\b(?:do|does|will|would) not (?:require|need) sponsorship\b"
                r"|\bno sponsorship\b", "", desired,
            )
            says_yes = bool(re.match(r"^(yes|true)\b", desired)) or any(
                phrase in affirmative for phrase in ("require sponsorship", "need sponsorship")
            )
            if requires is True and says_no:
                return _err(
                    f"select_option {ref}: refused false {region} no-sponsorship "
                    "claim; structured profile says requires_sponsorship=true"
                )
            if requires is False and says_yes:
                return _err(
                    f"select_option {ref}: refused false {region} sponsorship "
                    "claim; structured profile says requires_sponsorship=false"
                )

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


CHOOSE_FROM_OPTIONS = "__applyd_choose_from_rendered_options__"


def _resolve_from_rendered_options(
    page: Page, ref: str, profile: dict[str, Any] | None, resume_text: str,
    job_locations: list[str] | None, company: str,
) -> str:
    """Bind a field with no profile mapping by reading its real options.

    Deterministic policy gets the first chance against the rendered labels;
    only an unclassified, low-risk question reaches the constrained chooser.
    Returns the chosen option text, or an ``error:`` asking for review.
    """
    if not profile:
        return _err(f"select_option {ref}: no profile; send to review")
    loc = _ref_locator(page, ref)
    label = str(loc.get_attribute("data-applyd-label") or "")
    options = _read_options(page) if loc.get_attribute("data-applyd-combobox-open") == "1" else []
    if not options:
        opened = open_dropdown(page, ref)
        if opened.startswith("error:"):
            return opened.replace("open_dropdown", "select_option", 1)
        options = _read_options(page)
    texts = _option_texts(options)
    rendered = {str(i): text for i, text in enumerate(texts)}
    required = (loc.get_attribute("aria-required") == "true" or loc.get_attribute("required") is not None
                or loc.get_attribute("data-applyd-required") == "true")
    answer = resolve_question(label, profile, rendered, job_locations=job_locations or (),
                              company=company, required=required)
    if answer.choice in rendered:
        return rendered[answer.choice]
    if answer.predicate != "unclassified":
        return _err(f"select_option {ref}: no deterministic option for {label[:120]!r} "
                    f"({answer.reason}). Handle this field explicitly, or send to review "
                    "if it needs a fact the profile does not contain.")
    from . import option_chooser
    from ..local_store import get_local_store
    try:
        store = get_local_store()
    except Exception:  # noqa: BLE001
        store = None
    choice = option_chooser.choose(label, texts, profile, resume_text=resume_text, store=store)
    if choice.index is None:
        return _err(f"select_option {ref}: no grounded option for {label[:120]!r} "
                    f"({choice.reason}). Handle this field explicitly, or send to review "
                    "if it needs a fact the profile does not contain.")
    print(f"[chooser] {label[:80]!r} -> {texts[choice.index]!r} "
          f"(evidence={choice.evidence[:60]!r}, cached={choice.cached})", file=sys.stderr, flush=True)
    return texts[choice.index]


def select_option(
    page: Page,
    ref: str,
    value: str,
    profile: dict[str, Any] | None = None,
    resume_text: str = "",
    job_locations: list[str] | None = None,
    company: str = "",
) -> str:
    """Open a dropdown, deterministically match ``value``, and select it.

    This collapses the common open/inspect/pick sequence into one model call.
    On an ambiguous or absent match, it leaves the field untouched and returns
    the real choices so the agent can decide explicitly on its next turn.
    """
    try:
        if value == CHOOSE_FROM_OPTIONS:
            value = _resolve_from_rendered_options(
                page, ref, profile, resume_text, job_locations, company)
            if value.startswith("error:"):
                return value
        guard = _select_profile_guard(
            page, ref, value, profile, resume_text, job_locations, company
        )
        if guard:
            return guard
        loc = _ref_locator(page, ref)
        initial_options: list[dict[str, str]] = []
        if loc.get_attribute("data-applyd-combobox-open") == "1":
            # Reuse choices exposed by the previous turn instead of toggling
            # the same Greenhouse control closed.
            page.evaluate(_OPTIONS_JS)
            initial_options = _read_options(page)
        if not initial_options:
            opened = open_dropdown(page, ref)
            if opened.startswith("error:"):
                # Typeaheads (School, Location) render nothing until typed
                # into: type the value and pick a real suggestion instead.
                if "no options rendered" in opened and loc.evaluate(
                        "el => el.tagName === 'INPUT' && el.getAttribute('role') === 'combobox'", timeout=3000):
                    typed = fill_autocomplete(page, ref, value)
                    return typed.replace("fill_autocomplete", "select_option", 1)
                return opened.replace("open_dropdown", "select_option", 1)
            initial_options = _read_options(page)
        # Inspect what the component actually rendered before typing anything.
        # Most answer dropdowns are short, and an exact/unique compatible choice
        # should be selected directly from their real labels.
        options = initial_options
        raw_field_label = str(loc.get_attribute("data-applyd-label") or "")
        bound_value = _field_specific_option_value(raw_field_label, value)
        matched = _match_option(options, bound_value)
        if matched is None and profile:
            rendered = {str(index): item.get("text", "") for index, item in enumerate(options)}
            answer = resolve_question(
                raw_field_label, profile, rendered, job_locations=job_locations or (),
                company=company,
                required=loc.get_attribute("aria-required") == "true"
                    or loc.get_attribute("required") is not None,
            )
            if answer.choice in rendered:
                matched = options[int(answer.choice)]
        if matched is None and profile:
            field_label = " ".join(
                re.sub(
                    r"[^a-z0-9]+", " ",
                    raw_field_label.casefold(),
                ).split()
            )
            if any(phrase in field_label for phrase in (
                "how did you hear", "referral source", "source did you hear",
                "how did you first learn about",
            )):
                fallbacks = (
                    (profile.get("application_policy") or {}).get(
                        "required_referral_source_fallbacks"
                    ) or []
                )
                for fallback in fallbacks:
                    matched = _match_option(options, str(fallback))
                    if matched is not None:
                        break

        # Searchable React/Greenhouse lists virtualize thousands of choices;
        # clicking alone exposes only the first screen (Aalborg, Aalto, ...),
        # so only when the intended value is absent do we search and inspect
        # the filtered real options again.
        searched = False
        if matched is None:
            tag = loc.evaluate("el => el.tagName.toLowerCase()", timeout=8000)
            role = loc.get_attribute("role")
            if tag == "input" and role == "combobox":
                # React/Greenhouse searchable catalogs update from keyboard
                # events. locator.fill(value) changes the DOM value directly,
                # which leaves the large School/Discipline catalogs unfiltered
                # on some forms. Clear first, then type as a user so the
                # component actually renders the matching real options.
                loc.click(timeout=8000)
                loc.fill("", timeout=8000)
                loc.press_sequentially(bound_value, delay=35, timeout=20000)
                page.wait_for_timeout(1200)
                page.evaluate(_OPTIONS_JS)
                options = _read_options(page)
                matched = _match_option(options, bound_value)
                searched = True
        if matched is None and profile and re.search(
                r"\b(?:school|university|college|institution)\b", raw_field_label, re.I):
            # The candidate's school is absent from a national catalog
            # ("OTHER (non-UK university)"): the form's explicit Other choice
            # is the truthful answer. Restore the unfiltered list first.
            if searched:
                loc.fill("", timeout=8000)
                page.wait_for_timeout(500)
                page.evaluate(_OPTIONS_JS)
                options = _read_options(page) or initial_options
            others = [o for o in (options or initial_options)
                      if re.match(r"^\s*other\b", str(o.get("text", "")), re.I)]
            if len(others) == 1:
                matched = others[0]
                searched = False
        if matched is None:
            # A failed Greenhouse catalog search leaves the combobox filtered
            # to zero options. Restore the unfiltered catalog before returning
            # so the next turn can safely choose a real fallback such as
            # ``Other``. Without this reset, clicking the toggle closes the
            # empty list and every subsequent open/select call fails.
            if searched:
                loc.fill("", timeout=8000)
                page.wait_for_timeout(500)
                page.evaluate(_OPTIONS_JS)
            visible = ", ".join(repr(o.get("text", "")) for o in initial_options[:30])
            filtered = ", ".join(repr(o.get("text", "")) for o in options[:30])
            search_note = f"; filtered options after search: {filtered}" if searched else ""
            return _err(
                f"select_option {ref}: no unambiguous match for {value!r}; "
                f"initial visible options: {visible}{search_note}. "
                "Inspect these labels and call open_dropdown on the next turn "
                "before choosing a different answer."
            )
        result = pick_option(
            page, matched["ref"], profile, company, job_locations
        )
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

def _resume_upload_guard(page: Page, ref: str) -> str | None:
    """Allow the runner-owned resume only in an explicitly labeled resume field."""
    loc = _ref_locator(page, ref)
    label = str(loc.get_attribute("data-applyd-label") or "").strip()
    details = loc.evaluate(
        """el => {
            const direct = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
            const scope = el.closest(
                'fieldset, .field, .application-field, [data-field], [class*="field"]'
            );
            return {
                id: el.id || '', name: el.getAttribute('name') || '',
                dataui: el.getAttribute('data-ui') || '',
                aria: el.getAttribute('aria-label') || '',
                direct: direct?.innerText || el.closest('label')?.innerText || '',
                context: (scope?.innerText || '').slice(0, 500)
            };
        }""",
        timeout=3000,
    )
    if not isinstance(details, dict):
        details = {}
    local_identity = " ".join(str(details.get(key) or "") for key in (
        "id", "name", "dataui", "aria", "direct",
    ))
    combined = " ".join((label, local_identity, str(details.get("context") or "")))
    normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", combined.casefold()).split())
    prohibited = re.search(
        r"\b(?:transcript|portfolio|writing sample|video|photo|assessment)\b",
        normalized,
    )
    resume_target = re.search(r"\b(?:resume|cv|curriculum vitae)\b", normalized)
    if prohibited or not resume_target:
        return _err(
            f"upload_resume {ref}: refused non-resume upload target {label!r}; "
            "required transcripts, portfolios, writing samples, videos, and "
            "other artifacts must enter review"
        )
    return None


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
    const frame = document.querySelector(
        'iframe[src*="hcaptcha"], iframe[src*="recaptcha"], iframe[src*="challenges.cloudflare.com"]');
    const tok = document.querySelector(
        '[name="h-captcha-response"], [name="g-recaptcha-response"], [name="cf-turnstile-response"]');
    // Workable renders Cloudflare Turnstile invisibly on submit: no iframe in
    // the light DOM, the submit button disappears while the form stays up.
    // Treat that as a pending challenge so the poll waits instead of
    // reporting a premature "unconfirmed".
    const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden'; };
    const turnstileLoaded = [...document.scripts].some(s => /challenges\.cloudflare\.com\/turnstile/.test(s.src));
    const submitVisible = [...document.querySelectorAll('button[type="submit"], input[type="submit"]')]
        .some(el => visible(el) && !/cookie/i.test(el.innerText || el.value || ''));
    const formVisible = [...document.querySelectorAll('form')].some(visible);
    const turnstilePending = turnstileLoaded && formVisible && !submitVisible;
    return {
        present: !!frame || turnstilePending,
        provider: frame ? (frame.src.includes('cloudflare') ? 'turnstile' : 'widget')
                        : (turnstilePending ? 'turnstile' : null),
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
    try:
        turnstile = page.evaluate(
            "() => [...document.scripts].some(s => /challenges\\.cloudflare\\.com\\/turnstile/.test(s.src))")
    except Exception:  # noqa: BLE001
        turnstile = False
    if turnstile:
        # Workable re-shows the button after a failed invisible Turnstile
        # check; no error, no confirmation, no email. That is a CAPTCHA gate,
        # not an ambiguous outcome to keep re-clicking.
        return _err(
            f"submit {ref}: captcha did not resolve within {SUBMIT_CAPTCHA_WAIT_SECONDS}s "
            "(Cloudflare Turnstile; form still visible, no confirmation)"
        )
    return _err(
        f"submit {ref}: submission outcome unconfirmed; form is still visible at {page.url}"
    )


def submit(page: Page, ref: str, test_mode: bool, verify_ctx: dict | None = None,
           profile: dict[str, Any] | None = None) -> str:
    if profile:
        try:
            companies = page.evaluate(r"""() => [...document.querySelectorAll('input,textarea')]
              .filter(e=>e.type!=='hidden' && (e.getBoundingClientRect().width || e.getBoundingClientRect().height))
              .map(e=>({label:e.getAttribute('data-applyd-label') || e.getAttribute('aria-label') || '', value:e.value || ''}))
              .filter(e=>e.label.toLowerCase().replace(/[^a-z0-9]+/g,' ').trim()==='current company' && e.value)""")
            expected_company = profile.get('current_company')
            for field in companies:
                if isinstance(expected_company, str) and field['value'].strip() != expected_company.strip():
                    return _err('submit: current company differs from the structured profile; submission not attempted')
            dates = page.evaluate(r"""() => [...document.querySelectorAll('input,textarea,select')]
              .filter(e=>e.type!=='hidden' && (e.getBoundingClientRect().width || e.getBoundingClientRect().height))
              .map(e=>({label:e.getAttribute('data-applyd-label') || e.getAttribute('aria-label') || '',
                        value:(e.tagName==='SELECT' ? e.selectedOptions[0]?.text : e.value) || '',
                        offered:e.tagName==='SELECT' ? [...e.options].map(o=>(o.text||'').trim()) : null,
                        nearestOk:e.getAttribute('data-applyd-nearest-ok') || ''}))
              .filter(e=>/graduat/i.test(e.label) && /date|month|year/i.test(e.label) && e.value)""")
            for field in dates:
                # A nearest-date choice the runner already verified at select
                # time (custom dropdowns expose no option list here).
                if (graduation_nearest_policy(profile) and field.get('nearestOk')
                        and _normalize_option_text(field['nearestOk'])
                        == _normalize_option_text(field['value'])):
                    continue
                error = _date_profile_guard(field['label'], field['value'], profile,
                                            offered=field.get('offered'))
                if error:
                    return error + '; submission not attempted'
        except Exception as exc:
            return _err(f'submit: could not verify graduation dates: {type(exc).__name__}; submission not attempted')
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
            and (
                re.search(r"\b(submit|send)\b", control.get("text") or "")
                or (
                    control.get("inForm")
                    and re.search(r"\bapply\b", control.get("text") or "")
                )
            )
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
        return click(page, args["ref"], profile, job_locations)
    if name == "fill":
        return fill(
            page, args["ref"], args["value"], profile, resume_text, job_locations
        )
    if name == "fill_many":
        return fill_many(page, args["fields"], profile, resume_text, job_locations)
    if name == "fill_autocomplete":
        try:
            value, _ = _grounded_fill_value(page, args['ref'], args['value'], profile, resume_text, job_locations)
        except _OptionalNarrativeField:
            return 'skipped: optional narrative field left unchanged'
        except ValueError as exc:
            return _err(f'fill_autocomplete {args["ref"]}: {exc}')
        return fill_autocomplete(page, args["ref"], value)
    if name == "click_many":
        return click_many(page, args["refs"], profile, job_locations)
    if name == "open_dropdown":
        return open_dropdown(page, args["ref"])
    if name == "inspect_dropdowns":
        return inspect_dropdowns(page, args["refs"])
    if name == "pick_option":
        return pick_option(
            page, args["option_ref"], profile, company, job_locations
        )
    if name == "select_option":
        return select_option(
            page, args["ref"], args["value"], profile, resume_text, job_locations,
            company,
        )
    if name == "upload_resume":
        guard = _resume_upload_guard(page, args["ref"])
        if guard:
            return guard
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
        result = submit(page, args["ref"], test_mode, verify_ctx=verify_ctx, profile=profile)
        if result.startswith("error:"):
            # ATS validation/spam errors commonly rerender the form and destroy
            # every data-applyd-ref. Returning a fresh snapshot prevents the
            # model from retrying a stale submit ref on the changed document.
            try:
                page.wait_for_timeout(500)
                refreshed = snapshot(page)
            except Exception as exc:
                # Preserve the original submission outcome. Never retry a
                # submission just because its browser disappeared afterward.
                result += f"\nDOM refresh unavailable: {type(exc).__name__}"
            else:
                result += "\nDOM refreshed after failed submit; use only these new refs:\n"
                result += refreshed
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
        "Before changing a freshly snapshotted form, confirm you inspected every visible required question. List all required labels that are answerable and only exact required labels from snapshot whose consequential facts are truly missing. Unstarred attachments are optional; the runner-bound resume is available. Creative/motivation questions and referral-source questions are never missing facts.",
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
        "inspect_dropdowns",
        "Inspect the real labels for several independent dropdowns in one call, then close them. Use when a form has 3 or more required dropdowns whose desired labels are unknown. The next turn should batch select_option calls with the exact labels returned. This tool returns labels, not pickable option refs.",
        {"refs": {"type": "array", "items": {"type": "string"}},},
        required=["refs"],
    ),
    _fn(
        "pick_option",
        "Click an option by its ref (returned from open_dropdown).",
        {"option_ref": {"type": "string"}},
        required=["option_ref"],
    ),
    _fn(
        "upload_resume",
        "Upload the runner-bound tailored resume only to a field explicitly labeled Resume or CV. Required transcripts, portfolios, writing samples, videos, and other artifacts must enter review.",
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
