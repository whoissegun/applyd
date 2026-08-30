from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from applyd.commands.trace import cmd_trace
from applyd.local_store import LocalStore
from applyd.models import Job


class TraceCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LocalStore(Path(self.temp.name) / "applyd.sqlite3")
        now = datetime.now(timezone.utc)
        self.job = Job(
            id="trace-job", source="test", external_id="trace-job",
            company="Example", title="Engineer", url="https://example.test/job",
            locations=["Toronto"], description="Test", first_seen_at=now,
            last_seen_at=now,
        )
        self.store.upsert([self.job])
        profile_hash = self.store.set_profile({"full_name": "Private Person"})
        self.store.set_evaluation(self.job.id, "eligible", [], profile_hash)
        pdf = Path(self.temp.name) / "resume.pdf"
        tex = Path(self.temp.name) / "resume.tex"
        pdf.write_bytes(b"%PDF-test")
        tex.write_text("resume", encoding="utf-8")
        self.store.save_tailored_resume(
            job_id=self.job.id, source_resume_hash="source", model="kimi",
            edit_plan={}, latex_path=str(tex), pdf_path=str(pdf), cost_usd=0,
        )
        application, attempt = self.store.start_apply_attempt(self.job.id, "kimi")
        self.attempt = attempt
        self.store.record_apply_trace_event(
            attempt, sequence=1, turn=None, event_type="run_started",
            payload={"model": "kimi", "test_mode": False,
                     "browser_provider": "local"},
        )
        self.store.record_apply_trace_event(
            attempt, sequence=2, turn=2, event_type="tool_call", name="fill",
            payload={"args": {"ref": "r1", "value_chars": 14}},
        )
        self.store.finish_apply_attempt(
            attempt, status="review", reason="gated:captcha", cost_usd=0.01,
            turn_count=3, tool_calls={"fill": 1},
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_timeline_is_readable_and_does_not_contain_profile_pii(self) -> None:
        args = argparse.Namespace(
            job_id=self.job.id, all=False, errors_only=False,
            compare=False, format="text",
        )
        out = io.StringIO()
        with patch("applyd.commands.trace.get_local_store", return_value=self.store), redirect_stdout(out):
            self.assertEqual(cmd_trace(args), 0)
        rendered = out.getvalue()
        self.assertIn("provider=local", rendered)
        self.assertIn("value_chars", rendered)
        self.assertNotIn("Private Person", rendered)


if __name__ == "__main__":
    unittest.main()
