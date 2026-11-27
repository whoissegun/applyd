"""Save emails to Gmail Drafts over IMAP. This module never sends mail."""
from __future__ import annotations

import imaplib
import os
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Optional

from .compose import Email

DRAFTS_FOLDER = os.environ.get("APPLYD_DRAFTS_FOLDER", '"[Gmail]/Drafts"')


def save_draft(email: Email, to: str, *, imap_factory=imaplib.IMAP4_SSL) -> Optional[str]:
    """Append a draft; returns None on success or an error string."""
    user = os.environ.get("APPLYD_IMAP_USER", "").strip()
    password = os.environ.get("APPLYD_IMAP_PASSWORD", "").strip()
    if not user or not password:
        return "APPLYD_IMAP_USER / APPLYD_IMAP_PASSWORD not set"
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = email.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(email.text)
    msg.add_alternative(email.html, subtype="html")
    try:
        imap = imap_factory(os.environ.get("APPLYD_IMAP_HOST", "imap.gmail.com"),
                            int(os.environ.get("APPLYD_IMAP_PORT", "993")))
        try:
            imap.login(user, password)
            status, _ = imap.append(DRAFTS_FOLDER, "(\\Draft)",
                                    imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        finally:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"[:200]
    return None if status == "OK" else f"IMAP append returned {status}"
