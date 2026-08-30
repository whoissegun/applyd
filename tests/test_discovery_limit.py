from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from applyd.commands.discover import (
    _attach_retrieval_route,
    _freshest_within_limit,
    _supported_jobs,
)
from applyd.models import Job


def _job(index: int, posted_at: datetime | None) -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        id=str(index), source="test", external_id=str(index), company="Example",
        title=f"Engineer {index}", url=f"https://example.com/{index}",
        posted_at=posted_at, first_seen_at=now, last_seen_at=now,
    )


class DiscoveryLimitTests(unittest.TestCase):
    def test_branded_greenhouse_url_gets_canonical_retrieval_route(self) -> None:
        item = _job(1, datetime.now(timezone.utc))
        item.external_id = "12345"
        item.url = "https://example.com/careers?gh_jid=12345"
        _attach_retrieval_route(item, "greenhouse", "example")
        self.assertEqual(
            item.raw["_applyd_retrieval_url"],
            "https://boards.greenhouse.io/example/jobs/12345",
        )

    def test_supported_filter_rejects_workday_and_keeps_greenhouse(self) -> None:
        now = datetime.now(timezone.utc)
        greenhouse = _job(1, now)
        greenhouse.url = "https://boards.greenhouse.io/example/jobs/123"
        workday = _job(2, now)
        workday.url = "https://example.wd5.myworkdayjobs.com/jobs/job/abc"
        kept, skipped = _supported_jobs([workday, greenhouse])
        self.assertEqual([job.id for job in kept], ["1"])
        self.assertEqual(skipped, 1)

    def test_keeps_freshest_jobs_within_remaining_global_limit(self) -> None:
        now = datetime.now(timezone.utc)
        jobs = [
            _job(1, now - timedelta(days=3)),
            _job(2, now),
            _job(3, now - timedelta(days=1)),
            _job(4, None),
        ]
        kept = _freshest_within_limit(jobs, written=1, limit=3)
        self.assertEqual([job.id for job in kept], ["2", "3"])

    def test_zero_limit_is_unlimited_but_still_freshness_sorted(self) -> None:
        now = datetime.now(timezone.utc)
        jobs = [_job(1, None), _job(2, now)]
        self.assertEqual(
            [job.id for job in _freshest_within_limit(jobs, written=0, limit=0)],
            ["2", "1"],
        )


if __name__ == "__main__":
    unittest.main()
