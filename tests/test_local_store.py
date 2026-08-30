from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from applyd.local_store import LocalStore
from applyd.models import Job


def make_job(job_id: str = "job-1") -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        id=job_id,
        source="test",
        external_id=job_id,
        company="Example",
        title="Software Engineer",
        url="https://example.com/jobs/1",
        locations=["Toronto, Canada"],
        description="Build reliable systems. 2+ years of experience required.",
        first_seen_at=now,
        last_seen_at=now,
    )


class LocalStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LocalStore(Path(self.temp.name) / "applyd.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_pipeline_state_and_apply_attempt(self) -> None:
        job = make_job()
        self.assertEqual(self.store.upsert([job]), (1, 0))
        self.assertEqual(self.store.upsert([job]), (0, 1))
        self.store.set_classification(
            job.id,
            {"seniority": {"value": "entry", "evidence": "Software Engineer"}},
            model="test-model",
        )
        profile_hash = self.store.set_profile({"name": "Candidate"})
        self.store.set_evaluation(job.id, "eligible", [], profile_hash)

        artifact = Path(self.temp.name) / "resume.pdf"
        artifact.write_bytes(b"%PDF-test")
        tex = Path(self.temp.name) / "resume.tex"
        tex.write_text("resume", encoding="utf-8")
        self.store.save_tailored_resume(
            job_id=job.id,
            source_resume_hash="resume-hash",
            model="kimi",
            edit_plan={"summary": "test"},
            latex_path=str(tex),
            pdf_path=str(artifact),
            cost_usd=0.01,
        )

        application, attempt_id = self.store.start_apply_attempt(job.id, "kimi")
        self.assertEqual(application["status"], "tailored")
        self.store.record_apply_trace_event(
            attempt_id,
            sequence=1,
            turn=0,
            event_type="tool_call",
            name="fill",
            payload={"args": {"ref": "r3", "value_chars": 10}},
        )
        self.store.finish_apply_attempt(
            attempt_id,
            status="tested",
            reason="test-mode confirmation",
            cost_usd=0.02,
            turn_count=4,
            tool_calls={"submit": 1},
        )
        updated = self.store.get_application_by_job(job.id)
        self.assertEqual(updated["status"], "tested")
        attempts = self.store.get_apply_attempts(job.id)
        self.assertEqual(attempts[0]["tool_calls"], {"submit": 1})
        trace = self.store.get_apply_trace(attempt_id)
        self.assertEqual(trace[0]["sequence"], 1)
        self.assertEqual(trace[0]["payload"]["args"]["value_chars"], 10)

    def test_infra_error_returns_application_to_tailored(self) -> None:
        job = make_job()
        self.store.upsert([job])
        profile_hash = self.store.set_profile({"name": "Candidate"})
        self.store.set_evaluation(job.id, "eligible", [], profile_hash)
        pdf = Path(self.temp.name) / "resume.pdf"
        pdf.write_bytes(b"pdf")
        tex = Path(self.temp.name) / "resume.tex"
        tex.write_text("tex")
        self.store.save_tailored_resume(
            job_id=job.id, source_resume_hash="x", model="kimi",
            edit_plan={}, latex_path=str(tex), pdf_path=str(pdf), cost_usd=0,
        )
        _, attempt_id = self.store.start_apply_attempt(job.id, "kimi")
        self.store.finish_apply_attempt(
            attempt_id, status="infra_error", reason="provider unavailable"
        )
        self.assertEqual(self.store.get_application_by_job(job.id)["status"], "tailored")

    def test_manual_review_does_not_require_tailoring(self) -> None:
        job = make_job()
        self.store.upsert([job])
        self.store.mark_application_review(job.id, "manual_only_ats:smartrecruiters")
        application = self.store.get_application_by_job(job.id)
        self.assertEqual(application["status"], "review")
        self.assertEqual(application["reason"], "manual_only_ats:smartrecruiters")
        self.assertIsNone(application["tailored_resume_id"])

    def test_profile_question_gaps_are_aggregated(self) -> None:
        self.store.record_profile_question_gap(
            label="Are you bound by a non-compete agreement?",
            category="legal",
            job_id="job-1",
            company="Example",
        )
        self.store.record_profile_question_gap(
            label="Are you bound by a non compete agreement",
            category="legal",
            job_id="job-2",
            company="Another",
        )
        rows = self.store.list_profile_question_gaps()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["occurrences"], 2)
        self.assertEqual(rows[0]["last_company"], "Another")

    def test_retailoring_does_not_reopen_applied_job(self) -> None:
        job = make_job()
        self.store.upsert([job])
        profile_hash = self.store.set_profile({"name": "Candidate"})
        self.store.set_evaluation(job.id, "eligible", [], profile_hash)
        pdf = Path(self.temp.name) / "resume.pdf"
        pdf.write_bytes(b"pdf")
        tex = Path(self.temp.name) / "resume.tex"
        tex.write_text("tex")
        kwargs = dict(
            job_id=job.id, source_resume_hash="x", model="kimi",
            edit_plan={}, latex_path=str(tex), pdf_path=str(pdf), cost_usd=0,
        )
        self.store.save_tailored_resume(**kwargs)
        _, attempt_id = self.store.start_apply_attempt(job.id, "kimi")
        self.store.finish_apply_attempt(attempt_id, status="applied", reason="confirmed")
        self.store.save_tailored_resume(**kwargs)
        self.assertEqual(self.store.get_application_by_job(job.id)["status"], "applied")
        with self.assertRaisesRegex(ValueError, "already marked applied"):
            self.store.start_apply_attempt(job.id, "kimi")


if __name__ == "__main__":
    unittest.main()
