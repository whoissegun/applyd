"""Post-application outreach: find the hiring contact, draft a short email.

Drafts only. Nothing here sends mail: drafts are appended to the user's
Gmail Drafts folder over IMAP for the user to review and send.
"""
from .pipeline import draft_outreach_for_job, outreach_settings

__all__ = ["draft_outreach_for_job", "outreach_settings"]
