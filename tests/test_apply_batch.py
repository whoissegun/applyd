from __future__ import annotations

import unittest
import argparse
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from applyd.local_store import LocalStore
from applyd.models import Job

from applyd.commands.apply_batch import (
    MANUAL_ONLY_ATS,
    _captcha_gate,
    _candidate_scan_limit,
    _eligible_evaluation,
    _ats_failure_total,
    _failure_category,
    _matches_prior_role,
    _primary_browser_provider,
    _similar_title,
    cmd_apply_batch,
)


class ApplyBatchPolicyTests(unittest.TestCase):
    def test_aliased_ats_posting_cannot_hide_an_unranked_prior_attempt(self) -> None:
        for status in ("applied", "review", "failed", "infra_error", "in_progress", "legacy_applied"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                store = LocalStore(root / "state.sqlite3")
                self.addCleanup(store.close)
                now = datetime.now(timezone.utc)
                old = Job(
                    id="old", external_id="posting-123", source="simplifyjobs",
                    company="Example", title="Software Engineer - Early Career",
                    url="https://jobs.ashbyhq.com/example/posting-123/application?embed=true",
                    first_seen_at=now, last_seen_at=now,
                )
                fresh = old.model_copy(update={
                    "id": "fresh", "source": "ashby", "company": "Example Platform Inc",
                    "title": "Software Engineer, Early Career",
                    "url": "https://jobs.ashbyhq.com/example/posting-123",
                })
                store.upsert([old, fresh])
                profile_hash = store.set_profile({})
                store.set_evaluation(fresh.id, "eligible", [], profile_hash)
                pdf = root / "resume.pdf"
                pdf.write_bytes(b"%PDF-1.4\ntest")
                store.save_tailored_resume(
                    job_id=old.id, source_resume_hash="test", model="test",
                    edit_plan={}, latex_path=str(root / "resume.tex"),
                    pdf_path=str(pdf), cost_usd=0,
                )
                if status == "legacy_applied":
                    with store.transaction() as conn:
                        conn.execute("UPDATE applications SET status='applied' WHERE job_id=?", (old.id,))
                else:
                    _, attempt = store.start_apply_attempt(old.id, "test")
                    if status != "in_progress":
                        store.finish_apply_attempt(attempt, status=status, reason="test")
                with store.transaction() as conn:
                    conn.execute("UPDATE jobs SET active=0 WHERE id=?", (old.id,))
                self.assertEqual([j.id for j in store.iter_prior_application_jobs()], [old.id])
                args = argparse.Namespace(
                    top=1, minimum_score=70, report=str(root / "report.json"),
                    test_mode=False, captcha_fallback="none", max_total_apply_cost_usd=0,
                    max_per_ats=100, ats_failure_limit=3,
                )
                ranked = [{"job_id": fresh.id, "company": fresh.company,
                           "title": fresh.title, "score": 85}]
                with patch("applyd.commands.apply_batch.load_env"), \
                     patch("applyd.commands.apply_batch.get_local_store", return_value=store), \
                     patch.object(store, "iter_ranked_matches", return_value=iter(ranked)), \
                     patch("applyd.commands.apply_batch.check_jobs_liveness") as live, \
                     patch("applyd.commands.apply_batch.cmd_tailor") as tailor, \
                     patch("applyd.commands.apply_batch.cmd_apply") as apply:
                    self.assertEqual(cmd_apply_batch(args), 2)
                    live.assert_not_called()
                    tailor.assert_not_called()
                    apply.assert_not_called()
                self.assertEqual(store.get_apply_attempts(fresh.id), [])
                store.close()

    def test_small_batch_scans_past_dense_prior_attempt_prefix(self) -> None:
        self.assertEqual(_candidate_scan_limit(13), 2000)
        self.assertEqual(_candidate_scan_limit(150), 3000)

    def test_batch_requires_current_eligible_evaluation(self) -> None:
        self.assertTrue(_eligible_evaluation({"decision": "eligible"}))
        self.assertFalse(_eligible_evaluation({"decision": "ineligible"}))
        self.assertFalse(_eligible_evaluation(None))

    def test_smartrecruiters_is_default_excluded(self) -> None:
        self.assertIn("smartrecruiters", MANUAL_ONLY_ATS)

    def test_captcha_is_the_only_fallback_gate(self) -> None:
        self.assertTrue(_captcha_gate({"status": "review", "reason": "gated:captcha"}))
        self.assertFalse(_captcha_gate({
            "status": "review", "reason": "gated:email_verification"
        }))
        self.assertFalse(_captcha_gate({"status": "failed", "reason": "gated:captcha"}))

    def test_similar_applied_role_is_not_selected_again(self) -> None:
        self.assertTrue(_similar_title(
            "Software Engineer New Grad",
            "Software Engineer New Grad - 2027 Start",
        ))
        self.assertFalse(_similar_title(
            "Software Engineer New Grad",
            "Product Marketing Manager",
        ))

    def test_cross_source_prior_attempt_is_not_selected_again(self) -> None:
        prior = [("dellfor technologies", "Entry-Level Java Developer")]
        self.assertTrue(_matches_prior_role(
            "DellFor Technologies", "Entry Level Java Developer", prior
        ))
        self.assertFalse(_matches_prior_role(
            "DellFor Technologies", "Product Manager", prior
        ))

    def test_only_platform_failures_trip_circuit_breaker(self) -> None:
        self.assertEqual(_failure_category({
            "status": "review", "reason": "gated:captcha"
        }), "captcha")
        self.assertEqual(_failure_category({
            "status": "failed", "reason": "runner exception: timeout"
        }), "runtime_failure")
        self.assertIsNone(_failure_category({
            "status": "review", "reason": "review:missing_info"
        }))

    def test_real_lever_starts_on_brightdata(self) -> None:
        self.assertEqual(
            _primary_browser_provider("lever", test_mode=False), "brightdata"
        )
        self.assertEqual(
            _primary_browser_provider("lever", test_mode=True), "local"
        )
        self.assertEqual(
            _primary_browser_provider("greenhouse", test_mode=False), "local"
        )

    def test_ats_failure_symptoms_aggregate(self) -> None:
        failures = {
            ("smartrecruiters", "runtime_failure"): 2,
            ("smartrecruiters", "tool_failure"): 1,
            ("greenhouse", "runtime_failure"): 1,
        }
        self.assertEqual(_ats_failure_total(failures, "smartrecruiters"), 3)


if __name__ == "__main__":
    unittest.main()
