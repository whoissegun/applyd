from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from applyd.eligibility import evaluate_job
from applyd.enrichment.extract import JobExtractor, normalize_facts
from applyd.models import Job
from applyd.commands.enrich import _is_credit_budget_error


def job(title: str = "Software Engineer", locations: list[str] | None = None) -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        id="j", source="test", external_id="j", company="Example",
        title=title, url="https://example.com/j", locations=locations or [],
        first_seen_at=now, last_seen_at=now,
    )


class ExtractionEligibilityTests(unittest.TestCase):
    def test_credit_budget_error_detection(self) -> None:
        class CreditError(Exception):
            status_code = 402

        self.assertTrue(_is_credit_budget_error(CreditError("payment required")))
        self.assertFalse(_is_credit_budget_error(RuntimeError("network timeout")))

    def test_unsupported_consequential_facts_are_neutralized(self) -> None:
        facts = normalize_facts(
            {
                "workplace": {"value": "hybrid", "evidence": "hybrid schedule"},
                "sponsorship": {
                    "value": "unavailable", "countries": ["US"],
                    "evidence": "we cannot sponsor",
                },
                "minimum_years_experience": {
                    "value": 4, "evidence": "4+ years of experience",
                },
            },
            title="Engineer",
            description="This role requires 4+ years of experience.",
        )
        self.assertEqual(facts["workplace"]["value"], "unspecified")
        self.assertEqual(facts["sponsorship"]["value"], "not_stated")
        self.assertEqual(facts["minimum_years_experience"]["value"], 4)

    def test_batch_extraction_returns_valid_jobs_and_reports_missing_ids(self) -> None:
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='''{
                "jobs": [{
                    "job_id": "one",
                    "facts": {
                        "seniority": {"value": "entry", "evidence": "Entry level"},
                        "hard_requirements": []
                    }
                }]
            }'''))],
            usage=SimpleNamespace(cost=0.01, prompt_tokens=100, completion_tokens=40),
        )
        client = Mock()
        client.chat.completions.create.return_value = response
        result = JobExtractor(client=client).extract_many([
            ("one", "Entry level Engineer", "Example", "Entry level role " * 40),
            ("two", "Data Analyst", "Example", "Analyze data " * 60),
        ])

        self.assertEqual(set(result.results), {"one"})
        self.assertEqual(result.missing_ids, ("two",))
        self.assertEqual(result.results["one"].facts["seniority"]["value"], "entry")
        self.assertEqual(result.cost_usd, 0.01)
        request = client.chat.completions.create.call_args.kwargs
        self.assertIn('"job_id": "one"', request["messages"][1]["content"])
        self.assertGreater(request["max_tokens"], 1400)

    def test_batch_extraction_rejects_invalid_envelope(self) -> None:
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"seniority": {}}'))],
            usage=SimpleNamespace(cost=0.0, prompt_tokens=1, completion_tokens=1),
        )
        client = Mock()
        client.chat.completions.create.return_value = response
        extractor = JobExtractor(client=client)
        with self.assertRaisesRegex(ValueError, "invalid jobs payload"):
            extractor.extract_many([
                ("one", "Engineer", "Example", "Description " * 100),
            ])

    def test_deterministic_sponsorship_block(self) -> None:
        facts = {
            "seniority": {"value": "entry", "evidence": "entry level"},
            "clearance": {"value": "not_stated"},
            "citizenship": {"value": "not_stated"},
            "sponsorship": {
                "value": "unavailable", "countries": ["US"],
                "evidence": "Applicants must already be authorized in the US.",
            },
            "minimum_years_experience": {"value": None},
        }
        profile = {
            "work_authorization": {
                "US": {"authorized": False, "requires_sponsorship": True}
            },
            "preferences": {"allowed_seniority": ["entry", "new_grad"]},
        }
        result = evaluate_job(job(locations=["New York, NY"]), facts, profile)
        self.assertEqual(result.decision, "ineligible")
        self.assertEqual(result.reasons[0]["code"], "sponsorship_unavailable")

    def test_unknowns_remain_eligible_by_default(self) -> None:
        facts = {
            "seniority": {"value": "unknown"},
            "clearance": {"value": "not_stated"},
            "citizenship": {"value": "not_stated"},
            "sponsorship": {"value": "not_stated"},
            "minimum_years_experience": {"value": None},
        }
        self.assertEqual(evaluate_job(job(), facts, {"preferences": {}}).decision, "eligible")

    def test_eu_region_authorization_applies_to_member_country(self) -> None:
        facts = {
            "seniority": {"value": "entry"},
            "clearance": {"value": "not_stated"},
            "citizenship": {"value": "not_stated"},
            "sponsorship": {
                "value": "unavailable", "countries": ["DE"],
                "evidence": "Applicants must already be authorized to work in Germany.",
            },
            "minimum_years_experience": {"value": None},
        }
        profile = {
            "work_authorization": {
                "EU": {"authorized": False, "requires_sponsorship": True}
            },
            "preferences": {},
        }
        result = evaluate_job(job(locations=["Berlin, Germany"]), facts, profile)
        self.assertEqual(result.decision, "ineligible")
        self.assertEqual(result.reasons[0]["country"], "DE")


if __name__ == "__main__":
    unittest.main()
