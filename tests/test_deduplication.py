from __future__ import annotations

import unittest
from datetime import datetime, timezone

from applyd.deduplication import deduplicate_jobs
from applyd.models import Job


def make_job(
    job_id: str,
    *,
    source: str,
    url: str,
    title: str = "Software Engineer",
    description: str = "Build Python services.",
    locations: list[str] | None = None,
) -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        id=job_id,
        source=source,
        external_id=job_id,
        company="Example",
        title=title,
        url=url,
        locations=locations or ["Toronto, Canada"],
        description=description,
        first_seen_at=now,
        last_seen_at=now,
    )


class DeduplicationTests(unittest.TestCase):
    def test_same_ats_identity_across_sources_is_grouped(self) -> None:
        jobs = [
            make_job(
                "simplify:1",
                source="simplifyjobs",
                url="https://job-boards.greenhouse.io/example/jobs/123?src=x",
            ),
            make_job(
                "greenhouse:example:123",
                source="greenhouse",
                url="https://example.com/careers?gh_jid=123",
            ).model_copy(update={
                "raw": {
                    "_applyd_retrieval_url":
                    "https://boards.greenhouse.io/example/jobs/123"
                }
            }),
        ]
        result = deduplicate_jobs(jobs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].job_id, "simplify:1")
        self.assertEqual(result[0].canonical_job_id, "greenhouse:example:123")
        self.assertEqual(result[0].method, "ats_identity")

    def test_exact_fingerprint_groups_unidentified_cross_source_rows(self) -> None:
        jobs = [
            make_job("source-a:1", source="source-a", url="https://a.test/1"),
            make_job("source-b:2", source="source-b", url="https://b.test/2"),
        ]
        result = deduplicate_jobs(jobs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].method, "exact_fingerprint")

    def test_same_title_is_not_enough(self) -> None:
        jobs = [
            make_job("a", source="a", url="https://a.test/1"),
            make_job(
                "b", source="b", url="https://b.test/2",
                description="Build an entirely different Java platform.",
            ),
        ]
        self.assertEqual(deduplicate_jobs(jobs), [])

    def test_exact_text_does_not_merge_different_stable_ats_ids(self) -> None:
        jobs = [
            make_job(
                "one", source="simplifyjobs",
                url="https://jobs.ashbyhq.com/example/first/application",
            ),
            make_job(
                "two", source="simplifyjobs",
                url="https://jobs.ashbyhq.com/example/second/application",
            ),
        ]
        self.assertEqual(deduplicate_jobs(jobs), [])


if __name__ == "__main__":
    unittest.main()
