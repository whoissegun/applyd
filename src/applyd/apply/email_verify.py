"""Read the ATS verification code emailed after a low-score submit.

When Greenhouse's invisible reCAPTCHA scores a session as bot-like it doesn't
hard-block — it emails a one-time security code and shows an inline code field
("enter the code, resubmit your application"). Completing that in-session is far
more reliable than fighting the score (see CLAUDE.md → Apply layer → reCAPTCHA).

The deployed worker CANNOT use the claude.ai Gmail connector (that's only in the
agent's session), so mail is read over IMAP. Config via env:
  APPLYD_IMAP_HOST      (default imap.gmail.com)
  APPLYD_IMAP_PORT      (default 993)
  APPLYD_IMAP_USER      the applicant's mailbox address
  APPLYD_IMAP_PASSWORD  a Gmail *App Password* (16 chars), NOT the account pw
If user/password are unset, `build_code_reader()` returns None and the caller
falls back to gated:email_verification (today's behavior) — nothing breaks.

Multi-tenant note: a single global IMAP mailbox only works while every apply
uses one contact email. Per-tenant email access (per-user OAuth, or a
platform-owned +alias inbox) is the SaaS path — deferred.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Optional, Protocol

_SENDER = "greenhouse-mail.io"
_SUBJECT_MARKER = "security code for your application"
# Code sits after "...application:" in plaintext and inside an <h1> in HTML.
# Greenhouse codes observed: 8 mixed-case alphanumerics (dpATo6NS, 3YGnHG5O).
_CODE_TEXT_RE = re.compile(r"application:\s*([A-Za-z0-9]{6,12})\b", re.IGNORECASE)
_CODE_H1_RE = re.compile(r"<h1[^>]*>\s*([A-Za-z0-9]{6,12})\s*</h1>", re.IGNORECASE)
# Skew: the email's Date can lag/lead our submit clock by a few seconds.
_AFTER_SKEW_SECONDS = 90


class CodeReader(Protocol):
    def fetch(
        self, company: Optional[str], after: datetime,
        timeout_s: float = 150.0, poll_s: float = 6.0,
    ) -> Optional[str]:
        """Poll for a security-code email newer than `after`, matching `company`
        when given. Returns the code, or None on timeout."""
        ...


def _log(msg: str) -> None:
    print(f"[email_verify] {msg}", file=sys.stderr, flush=True)


def _body_text(msg: Message) -> tuple[str, str]:
    """Return (plaintext, html) across single/multipart messages."""
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            try:
                payload = part.get_payload(decode=True) or b""
                text = payload.decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
            if ctype == "text/plain":
                plain += text
            else:
                html += text
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            text = payload.decode(msg.get_content_charset() or "utf-8", "replace")
        except Exception:
            text = ""
        if msg.get_content_type() == "text/html":
            html = text
        else:
            plain = text
    return plain, html


def _extract_code(msg: Message) -> Optional[str]:
    plain, html = _body_text(msg)
    for pattern, src in ((_CODE_TEXT_RE, plain), (_CODE_H1_RE, html), (_CODE_TEXT_RE, html)):
        m = pattern.search(src or "")
        if m:
            return m.group(1)
    return None


def _company_matches(subject: str, company: Optional[str]) -> bool:
    if not company:
        return True
    subj = subject.lower()
    # Subject: "Security code for your application to <Company>". Match on the
    # longest alnum token of the company name to tolerate "Inc"/"Technologies".
    tokens = [t for t in re.split(r"[^a-z0-9]+", company.lower()) if len(t) >= 3]
    if not tokens:
        return True
    return any(t in subj for t in tokens)


class ImapCodeReader:
    def __init__(self, host: str, port: int, user: str, password: str) -> None:
        self._host, self._port, self._user, self._password = host, port, user, password

    def fetch(
        self, company: Optional[str], after: datetime,
        timeout_s: float = 150.0, poll_s: float = 6.0,
    ) -> Optional[str]:
        deadline = time.time() + timeout_s
        attempts = 0
        while time.time() < deadline:
            attempts += 1
            try:
                code = self._search_once(company, after)
            except Exception as e:  # noqa: BLE001
                _log(f"IMAP search failed ({type(e).__name__}: {str(e)[:120]}); retrying")
                code = None
            if code:
                _log(f"got code for company={company!r} after {attempts} poll(s)")
                return code
            time.sleep(poll_s)
        _log(f"timed out after {timeout_s:.0f}s waiting for code (company={company!r})")
        return None

    def _search_once(self, company: Optional[str], after: datetime) -> Optional[str]:
        imap = imaplib.IMAP4_SSL(self._host, self._port)
        try:
            imap.login(self._user, self._password)
            imap.select("INBOX")
            # IMAP SINCE is date-granular AND evaluated in the server's timezone,
            # not UTC. An email that arrived at 00:59 UTC can still be "yesterday"
            # on a server behind UTC, so `SINCE <utc-today>` silently excludes it
            # (this dropped every code for applies in the ~00:00–08:00 UTC window).
            # Subtract a day for margin; the precise `sent >= after` time-filter
            # below still guarantees we never accept a stale pre-submit code.
            since = (after - timedelta(days=1)).strftime("%d-%b-%Y")
            typ, data = imap.search(None, f'(FROM "{_SENDER}" SINCE "{since}")')
            if typ != "OK" or not data or not data[0]:
                return None
            ids = data[0].split()
            best: tuple[datetime, str] | None = None
            for mid in reversed(ids[-25:]):  # newest first, cap work
                typ, msg_data = imap.fetch(mid, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                subject = str(msg.get("Subject", ""))
                if _SUBJECT_MARKER not in subject.lower():
                    continue
                try:
                    sent = parsedate_to_datetime(msg.get("Date", ""))
                    if sent.tzinfo is None:
                        sent = sent.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if (sent - after).total_seconds() < -_AFTER_SKEW_SECONDS:
                    continue  # stale code from a prior attempt
                if not _company_matches(subject, company):
                    continue
                code = _extract_code(msg)
                if code and (best is None or sent > best[0]):
                    best = (sent, code)
            return best[1] if best else None
        finally:
            try:
                imap.logout()
            except Exception:
                pass


def build_code_reader() -> Optional[CodeReader]:
    """Construct the configured reader, or None if IMAP creds are unset."""
    user = os.environ.get("APPLYD_IMAP_USER", "").strip()
    password = os.environ.get("APPLYD_IMAP_PASSWORD", "").strip()
    if not user or not password:
        return None
    return ImapCodeReader(
        host=os.environ.get("APPLYD_IMAP_HOST", "imap.gmail.com"),
        port=int(os.environ.get("APPLYD_IMAP_PORT", "993")),
        user=user,
        password=password,
    )
