from __future__ import annotations

import unittest

from applyd.commands.apply_batch import (
    MANUAL_ONLY_ATS,
    _captcha_gate,
    _ats_failure_total,
    _failure_category,
    _matches_prior_role,
    _primary_browser_provider,
    _similar_title,
)


class ApplyBatchPolicyTests(unittest.TestCase):
    def test_smartrecruiters_is_manual_only(self) -> None:
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
