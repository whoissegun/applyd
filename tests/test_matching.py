from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from applyd.matching import (
    candidate_embedding_text,
    extract_known_skills,
    score_match,
)
from applyd.models import Job


def make_job(*, title: str, description: str) -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        id="job-1",
        source="greenhouse",
        external_id="1",
        company="Example",
        title=title,
        url="https://boards.greenhouse.io/example/jobs/1",
        locations=["Toronto, Canada"],
        posted_at=now - timedelta(days=2),
        description=description,
        first_seen_at=now,
        last_seen_at=now,
    )


class MatchingTests(unittest.TestCase):
    def test_candidate_embedding_omits_legal_and_demographic_profile_data(self) -> None:
        text = candidate_embedding_text(
            {
                "experience": [{
                    "title": "Software Engineer Intern",
                    "company": "Example",
                    "bullets": [{"text": "Built Python services"}],
                }],
                "skills": {"Languages": ["Python"]},
            },
            {
                "citizenships": ["NG"],
                "ethnicity": "Black or African American",
                "matchmaking": {
                    "target_role_families": ["software_engineering"],
                    "preferred_seniority": ["new_grad"],
                },
            },
        )
        self.assertIn("software engineering", text)
        self.assertIn("Built Python services", text)
        self.assertNotIn("NG", text)
        self.assertNotIn("Black or African American", text)

    def test_skill_aliases_are_boundary_aware(self) -> None:
        found = extract_known_skills("React, TypeScript, PostgreSQL and Apache Kafka")
        self.assertTrue({"react", "typescript", "postgresql", "kafka"} <= found)
        self.assertNotIn("rest", extract_known_skills("interesting systems"))

    def test_reranker_rewards_fit_and_exposes_components(self) -> None:
        result = score_match(
            make_job(
                title="Machine Learning Engineer, New Grad",
                description="Build Python and PyTorch services using Apache Kafka.",
            ),
            {
                "role_family": {"value": "machine_learning"},
                "seniority": {"value": "new_grad"},
                "hard_requirements": [],
            },
            semantic_similarity=0.9,
            candidate_skills={"python", "pytorch", "kafka"},
            profile={
                "matchmaking": {
                    "target_role_families": ["machine_learning"],
                    "preferred_seniority": ["new_grad", "entry"],
                    "stretch_seniority": ["mid"],
                }
            },
        )
        self.assertEqual(result.band, "excellent")
        self.assertGreaterEqual(result.score, 85)
        self.assertEqual(
            result.components["technology_overlap"]["matched"],
            ["kafka", "python", "pytorch"],
        )
        self.assertEqual(
            result.components["application_readiness"]["ats"], "greenhouse"
        )


if __name__ == "__main__":
    unittest.main()
