from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from applyd.discovery import ATS_MODULES
from applyd.liveness import check_jobs_liveness
from applyd.models import Job


def make_job(job_id: str, *, raw: dict | None = None) -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        id=f"greenhouse:example:{job_id}",
        source="greenhouse",
        external_id=job_id,
        company="Example",
        title="Software Engineer",
        url=f"https://boards.greenhouse.io/example/jobs/{job_id}",
        locations=["Toronto, Canada"],
        raw=raw or {},
        first_seen_at=now,
        last_seen_at=now,
    )


class LivenessTests(unittest.TestCase):
    def test_one_board_fetch_checks_multiple_jobs(self) -> None:
        module = Mock()
        module.fetch.return_value = [make_job("one")]
        with patch.dict(ATS_MODULES, {"greenhouse": module}, clear=True):
            results = check_jobs_liveness(
                [make_job("one"), make_job("two")], workers=2
            )
        self.assertEqual([value.status for value in results], ["live", "closed"])
        module.fetch.assert_called_once()

    def test_stored_past_deadline_closes_without_board_signal(self) -> None:
        module = Mock()
        module.fetch.return_value = [make_job("one")]
        expired = make_job("one", raw={"application_deadline": "2020-01-01"})
        with patch.dict(ATS_MODULES, {"greenhouse": module}, clear=True):
            result = check_jobs_liveness([expired], workers=1)[0]
        self.assertEqual(result.status, "closed")
        self.assertEqual(result.method, "application_deadline")


if __name__ == "__main__":
    unittest.main()
