from __future__ import annotations

from contextlib import closing
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from applyd.dashboard.server import DashboardData, DashboardServer
from applyd.local_store import LocalStore
from test_local_store import make_job


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "applyd.sqlite3"
        self.store = LocalStore(self.db)
        self.store.upsert([make_job()])
        self.pdf = self.root / "resume.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\noriginal resume")
        self.tex = self.root / "resume.tex"
        self.tex.write_text("resume")
        self.tailor()
        self.data = DashboardData(self.db, self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def tailor(self):
        return self.store.save_tailored_resume(
            job_id="job-1", source_resume_hash="source", model="test",
            edit_plan={}, latex_path=str(self.tex), pdf_path=str(self.pdf), cost_usd=0,
        )

    def test_snapshot_matches_upload_and_survives_retailoring(self):
        application, attempt = self.store.start_apply_attempt("job-1", "test")
        saved = Path(application["pdf_path"])
        self.assertNotEqual(saved, self.pdf)
        self.assertEqual(saved.read_bytes(), self.pdf.read_bytes())
        self.store.finish_apply_attempt(attempt, status="applied", reason="confirmed")
        finished = self.store.get_apply_attempts("job-1")[0]["finished_at"]
        self.pdf.write_bytes(b"%PDF-1.4\nchanged resume")
        self.tailor()
        row = self.data.payload()["applications"][0]
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["applied_at"], finished)
        self.assertEqual(row["resume_provenance"], "snapshot")
        self.assertEqual(saved.read_bytes(), b"%PDF-1.4\noriginal resume")
        self.assertNotIn(str(self.root), json.dumps(row))

    def test_review_test_and_interrupted_are_not_applied(self):
        for status in ("review", "tested", "infra_error"):
            _, attempt = self.store.start_apply_attempt("job-1", "test")
            self.store.finish_apply_attempt(attempt, status=status, reason="not submitted")
            row = self.data.payload()["applications"][0]
            self.assertEqual(row["status"], status)
            self.assertIsNone(row["applied_at"])

    def test_successful_attempt_remains_authority_for_date_and_resume(self):
        _, failed = self.store.start_apply_attempt("job-1", "test")
        self.store.finish_apply_attempt(failed, status="failed", reason="failed")
        _, success = self.store.start_apply_attempt("job-1", "test")
        self.store.finish_apply_attempt(success, status="applied", reason="confirmed")
        row = self.data.records()[0]
        self.assertEqual(row["attempt_count"], 2)
        self.assertEqual(Path(row["_pdf"]).name, success + ".pdf")
        self.assertEqual(row["applied_at"], self.store.get_apply_attempts("job-1")[-1]["finished_at"])

    def test_legacy_database_is_read_without_migration(self):
        _, attempt = self.store.start_apply_attempt("job-1", "test")
        self.store.finish_apply_attempt(attempt, status="applied", reason="confirmed")
        with self.store.transaction() as conn:
            conn.execute("DROP TABLE apply_resume_artifacts")
        row = self.data.records()[0]
        self.assertEqual(row["resume_provenance"], "legacy")
        self.assertTrue(row["resume_available"])
        self.pdf.unlink()
        self.assertFalse(self.data.records()[0]["resume_available"])
        with closing(self.data.connect()) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='apply_resume_artifacts'").fetchone())

    def test_missing_database_is_empty_and_not_created(self):
        path = self.root / "missing.sqlite3"
        payload = DashboardData(path).payload()
        self.assertEqual(payload["applications"], [])
        self.assertFalse(payload["database_exists"])
        self.assertFalse(path.exists())

    def test_http_access_assets_and_pdf_integrity(self):
        application, attempt = self.store.start_apply_attempt("job-1", "test")
        self.store.finish_apply_attempt(attempt, status="applied", reason="confirmed")
        server = DashboardServer(("127.0.0.1", 0), self.data)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def get(path, headers=None, method="GET"):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            result = response.status, dict(response.getheaders()), response.read()
            conn.close()
            return result
        try:
            for asset in ("/", "/app.js", "/app.css", "/api/applications"):
                self.assertEqual(get(asset)[0], 200)
            url = self.data.payload()["applications"][0]["resume_url"]
            status, headers, body = get(url)
            self.assertEqual(status, 200)
            self.assertEqual(body, self.pdf.read_bytes())
            self.assertEqual(headers["Content-Type"], "application/pdf")
            self.assertTrue(get(url + "?download=1")[1]["Content-Disposition"].startswith("attachment"))
            self.assertEqual(get("/api/applications", {"Host": "attacker.example"})[0], 403)
            self.assertEqual(get("/api/applications", {"Origin": "https://attacker.example"})[0], 403)
            self.assertEqual(get("/../profile.json")[0], 404)
            self.assertEqual(get("/api/applications/bad/resume")[0], 404)
            self.assertEqual(get("/api/applications", method="POST")[0], 501)
            Path(application["pdf_path"]).write_bytes(b"%PDF-1.4\ntampered")
            self.assertEqual(get(url)[0], 409)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
