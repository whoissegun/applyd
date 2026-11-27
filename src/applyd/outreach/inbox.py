"""Detect a rejection for a specific application in the user's inbox (IMAP, read-only).

A rejection must (1) arrive after the application, (2) name the company,
(3) state a decision ("decided not to move forward / progress", "proceed
with other candidates"), and (4) either name this role or name no role.
Phrases like "unfortunately we can't respond to everyone" in application
acknowledgements are not decisions and never count.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from typing import Optional

DECISION = re.compile(
    r"decided (?:not to|to not) (?:move forward|progress|proceed|pursue|advance)|"
    r"not (?:to )?(?:be )?(?:moving|move) forward with your (?:application|candidacy)|"
    r"(?:will|would) not be (?:moving forward|proceeding|progressing)|"
    r"(?:decided to|chosen to|chose to|will) (?:move forward|proceed|pursue) with (?:other|another|a different) "
    r"(?:candidate|applicant|individual)s?|"
    r"(?:have not|haven't|were not|was not) (?:been )?selected|"
    r"regret to inform|unable to offer you|not (?:be )?offering you|"
    r"position has (?:now )?been filled|no longer (?:considering|being considered)",
    re.I,
)
ROLE_PHRASE = re.compile(
    r"(?:for|to) (?:the|our) (.{4,90}?) (?:role|position|opening|opportunity)\b", re.I)
_STOP = {"the", "a", "an", "and", "of", "for", "to", "at", "in", "i", "ii", "iii", "1", "2",
         "role", "position", "engineer", "software", "new", "grad"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.casefold()) if w not in _STOP and len(w) > 1}


def _company_key(company: str) -> str:
    return re.sub(r"[^a-z0-9]", "", company.casefold())


def is_rejection_for(subject: str, sender: str, body: str, *, company: str, title: str) -> bool:
    # Emails wrap lines mid-phrase ("decided not to\nprogress"); match on
    # normalized whitespace and straight apostrophes.
    text = " ".join(f"{subject}\n{body}".replace("’", "'").split())
    flat = re.sub(r"[^a-z0-9]", "", f"{subject} {sender} {body[:3000]}".casefold())
    if _company_key(company)[:6] not in flat or not DECISION.search(text):
        return False
    named = [m.group(1) for m in ROLE_PHRASE.finditer(text)]
    if not named:
        return True                      # company-level decision after applying
    want = _words(title)
    if not want:
        return True
    for role in named:
        got = _words(role)
        if got and len(want & got) / len(want) >= 0.6:
            return True
        if not got and re.search(r"software engineer", role, re.I) and re.search(r"software engineer", title, re.I):
            return True
    return False


def _decode(value: Optional[str]) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:  # noqa: BLE001
        return value or ""


def _plain_text(msg: email.message.Message) -> str:
    parts = msg.walk() if msg.is_multipart() else [msg]
    texts, htmls = [], []
    for part in parts:
        ctype = part.get_content_type()
        if ctype not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        (texts if ctype == "text/plain" else htmls).append(text)
    if texts:
        return "\n".join(texts)
    return re.sub(r"<[^>]+>", " ", "\n".join(htmls))


class RejectionChecker:
    """Read-only inbox search. Uses Gmail's X-GM-RAW search when available."""

    def __init__(self, imap_factory=imaplib.IMAP4_SSL) -> None:
        self.user = os.environ.get("APPLYD_IMAP_USER", "").strip()
        self.password = os.environ.get("APPLYD_IMAP_PASSWORD", "").strip()
        self.host = os.environ.get("APPLYD_IMAP_HOST", "imap.gmail.com")
        self.port = int(os.environ.get("APPLYD_IMAP_PORT", "993"))
        self._factory = imap_factory

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password)

    def find(self, *, company: str, title: str, applied_at: datetime) -> Optional[str]:
        """Subject of a rejection for this application, or None."""
        if not self.configured:
            return None
        since = (applied_at - timedelta(days=1)).strftime("%Y/%m/%d")
        name = re.sub(r'["\\]', " ", company).strip()
        imap = self._factory(self.host, self.port)
        try:
            imap.login(self.user, self.password)
            status, _ = imap.select('"[Gmail]/All Mail"', readonly=True)
            if status != "OK":
                imap.select("INBOX", readonly=True)
            query = f'after:{since} "{name}" -in:sent -in:drafts'
            quoted = '"' + query.replace("\\", "\\\\").replace('"', '\\"') + '"'
            status, data = imap.search(None, "X-GM-RAW", quoted)
            ids = (data[0] or b"").split() if status == "OK" else []
            for msg_id in ids[-25:]:
                status, fetched = imap.fetch(msg_id, "(BODY.PEEK[])")
                if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                    continue
                msg = email.message_from_bytes(fetched[0][1])
                subject, sender = _decode(msg.get("Subject")), _decode(msg.get("From"))
                if is_rejection_for(subject, sender, _plain_text(msg), company=company, title=title):
                    return subject or "(no subject)"
        finally:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001
                pass
        return None
