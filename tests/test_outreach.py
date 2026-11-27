from __future__ import annotations

import unittest
from types import SimpleNamespace

from applyd.outreach.compose import compose, role_phrase
from applyd.outreach.contacts import Contact, domain_from_description
from applyd.outreach.drafts import save_draft
from applyd.outreach.pipeline import draft_outreach_for_job

SETTINGS = {
    "enabled": True,
    "subject": "i'll be brief - applied for {role}",
    "greeting": "hey {first_name}!",
    "intro": "i'm Divine, a 4th year cs student @ Carleton university.",
    "applied": "i just applied for the {role} role. some stuff i've done:",
    "bullets": ["ML eng intern at Shopify", "SWE intern at Lyft", "building Spequlo"],
    "closing": ["i think i'd be a great fit.", "if you're open, would love to chat."],
    "signoff": "best,",
    "signature_name": "Divine",
    "signature_url": "https://www.linkedin.com/in/example/",
}


class FakeStore:
    def __init__(self, prior=None):
        self.rows, self.prior = [], prior

    def outreach_for_company(self, key):
        return self.prior

    def outreach_for_job(self, job_id):
        return None

    def record_outreach(self, job_id, key, status, **fields):
        self.rows.append((job_id, key, status, fields))


class FakeFinder:
    name = "fake"

    def __init__(self, contact):
        self.contact, self.calls = contact, 0

    def find(self, **_):
        self.calls += 1
        return self.contact


JOB = SimpleNamespace(id="j1", company="Keplar", title="Software Engineer New Grad",
                      description="Questions? careers@keplar.io")
FOUND = Contact("found", first_name="Dhruv", full_name="Dhruv G", title="CEO",
                email="dhruv@keplar.io", email_status="valid", source="reports_to", credits_used=1)


class ComposeTests(unittest.TestCase):
    def test_template_matches_user_style(self) -> None:
        email = compose(SETTINGS, company="Keplar", title="Software Engineer New Grad",
                        first_name="Dhruv", company_sentence="really like what you guys are doing at Keplar.")
        self.assertEqual("i'll be brief - applied for software engineer new grad", email.subject)
        self.assertTrue(email.text.startswith("hey Dhruv!\n\ni'm Divine, a 4th year cs student"))
        self.assertIn("- ML eng intern at Shopify\n- SWE intern at Lyft\n- building Spequlo", email.text)
        self.assertIn('<a href="https://www.linkedin.com/in/example/">Divine</a>', email.html)
        self.assertNotIn("—", email.text)

    def test_role_phrase(self) -> None:
        self.assertEqual("innovation software engineer 1, marketplace",
                         role_phrase("Innovation Software Engineer 1 - Marketplace"))
        self.assertEqual("software engineer intern", role_phrase("Software Engineer Intern (Summer 2027)"))

    def test_fallback_company_line(self) -> None:
        email = compose({**SETTINGS, "fallback_company_line": "i think what you guys do at {company} is really cool."},
                        company="Acme", title="SWE", first_name="Al", company_sentence=None)
        self.assertIn("i think what you guys do at Acme is really cool.", email.text)

    def test_domain_from_posting_email(self) -> None:
        self.assertEqual("kaluza.com", domain_from_description("email careers@kaluza.com", "Kaluza"))
        self.assertIsNone(domain_from_description("apply at jobs@greenhouse.io", "Kaluza"))


class PipelineTests(unittest.TestCase):
    def run_pipeline(self, store, finder, settings=SETTINGS):
        saved = []
        summary = draft_outreach_for_job(
            store, JOB, {"outreach": settings}, finder=finder, llm=None,
            save=lambda email, to: saved.append((email, to)) or None,
            rejection_checker=SimpleNamespace(configured=False))
        return summary, saved

    def test_drafts_verified_contact(self) -> None:
        store, finder = FakeStore(), FakeFinder(FOUND)
        summary, saved = self.run_pipeline(store, finder)
        self.assertEqual("drafted", summary["status"])
        self.assertEqual("dhruv@keplar.io", saved[0][1])
        self.assertEqual("drafted", store.rows[0][2])

    def test_one_contact_per_company(self) -> None:
        store, finder = FakeStore(prior={"status": "drafted"}), FakeFinder(FOUND)
        summary, saved = self.run_pipeline(store, finder)
        self.assertEqual("skipped_company_done", summary["status"])
        self.assertEqual(0, finder.calls)
        self.assertEqual([], saved)

    def test_unverified_email_not_drafted_by_default(self) -> None:
        contact = Contact("found", first_name="A", full_name="A B", email="a@keplar.io",
                          email_status="catch_all", credits_used=1)
        store = FakeStore()
        summary, saved = self.run_pipeline(store, FakeFinder(contact))
        self.assertEqual("unverified_email", summary["status"])
        self.assertEqual([], saved)
        summary, saved = self.run_pipeline(FakeStore(), FakeFinder(contact),
                                           {**SETTINGS, "allow_catch_all_emails": True})
        self.assertEqual("drafted", summary["status"])

    def test_disabled_does_nothing(self) -> None:
        finder = FakeFinder(FOUND)
        summary, saved = self.run_pipeline(FakeStore(), finder, {**SETTINGS, "enabled": False})
        self.assertEqual("disabled", summary["status"])
        self.assertEqual(0, finder.calls)


class DraftTests(unittest.TestCase):
    def test_appends_to_drafts_and_never_sends(self) -> None:
        import os
        calls = []

        class FakeIMAP:
            def __init__(self, host, port):
                calls.append(("connect", host))

            def login(self, user, password):
                calls.append(("login", user))

            def append(self, folder, flags, date, message):
                calls.append(("append", folder, flags, b"Subject: hi" in message))
                return "OK", [b""]

            def logout(self):
                calls.append(("logout",))

        env = {"APPLYD_IMAP_USER": "me@example.test", "APPLYD_IMAP_PASSWORD": "x"}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            email = compose(SETTINGS, company="K", title="SWE", first_name="A", company_sentence=None)
            email = type(email)(subject="hi", text=email.text, html=email.html)
            self.assertIsNone(save_draft(email, "a@k.test", imap_factory=FakeIMAP))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(("append", '"[Gmail]/Drafts"', "(\\Draft)", True), calls[2])
        self.assertFalse(any(c[0] == "send" for c in calls))


class RejectionDetectionTests(unittest.TestCase):
    def test_real_rejection_wordings(self) -> None:
        from applyd.outreach.inbox import is_rejection_for
        self.assertTrue(is_rejection_for(
            "Your application to Kaluza - Software Engineer", "talent@kaluza.com",
            "Thank you for your interest in Kaluza and the Software Engineer role. After reviewing your "
            "application, we've unfortunately decided not to progress it this time around.",
            company="Kaluza", title="Software Engineer"))
        self.assertTrue(is_rejection_for(
            "Important information about your application to StubHub", "no-reply@stubhub.com",
            "we have decided not to move forward with your application for the Software Engineer I "
            "(New Grad) - Consumer Experience role at this time.",
            company="StubHub", title="Software Engineer New Grad - Consumer Experience"))

    def test_acknowledgement_is_not_rejection(self) -> None:
        from applyd.outreach.inbox import is_rejection_for
        self.assertFalse(is_rejection_for(
            "Thanks for applying to Cohere!", "no-reply@ashbyhq.com",
            "Thank you for applying to the Member of Technical Staff, Search role at Cohere! Unfortunately, "
            "due to the high volume of interest we aren't able to personally respond to every candidate.",
            company="cohere", title="Member of Technical Staff, Search"))

    def test_rejection_for_a_different_role_does_not_count(self) -> None:
        from applyd.outreach.inbox import is_rejection_for
        body = ("thank you for your interest in Affirm and giving us the opportunity to consider you for the "
                "Software Engineer I, Fullstack (Search) role. Unfortunately, we have decided not to move "
                "forward with your candidacy at this time.")
        self.assertFalse(is_rejection_for("Follow-up on your application to Affirm", "gem", body,
                                          company="Affirm", title="Software Engineer 1 - Backend - Streaming Infra"))
        self.assertTrue(is_rejection_for("Follow-up on your application to Affirm", "gem", body,
                                         company="Affirm", title="Software Engineer I, Fullstack (Search)"))

    def test_pipeline_skips_rejected_application(self) -> None:
        store, finder = FakeStore(), FakeFinder(FOUND)
        store.get_application_by_job = lambda job_id: {"claimed_at": "2026-11-21T00:00:00+00:00"}
        checker = SimpleNamespace(configured=True, find=lambda **_: "Your application to Keplar")
        summary = draft_outreach_for_job(store, JOB, {"outreach": SETTINGS}, finder=finder,
                                         save=lambda *_: None, rejection_checker=checker)
        self.assertEqual("rejected", summary["status"])
        self.assertEqual(0, finder.calls)
        self.assertEqual("rejected", store.rows[0][2])


class WrappedLineRejectionTests(unittest.TestCase):
    def test_line_wrapped_decision_phrase(self) -> None:
        from applyd.outreach.inbox import is_rejection_for
        self.assertTrue(is_rejection_for(
            "Your application to Kaluza - Software Engineer", "x@kaluza.com",
            "After reviewing your application, we’ve unfortunately decided not to\nprogress it this time around.",
            company="Kaluza", title="Software Engineer"))
