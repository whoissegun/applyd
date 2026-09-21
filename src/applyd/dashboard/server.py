"""Loopback-only, read-only HTTP view of the local application ledger.

No frontend build step, external assets, model calls, or application actions.
The reader supports pre-snapshot databases without migrating them on launch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import webbrowser
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from ..config import load_env
from ..local_store import default_db_path


class DashboardData:
    def __init__(self, db: Path, root: Path | None = None):
        self.db = db.resolve()
        self.root = (root or Path.cwd()).resolve()

    def connect(self):
        conn = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn

    def records(self) -> list[dict]:
        if not self.db.exists():
            return []
        with closing(self.connect()) as conn:
            applications = conn.execute("""
                SELECT a.id, a.job_id, a.status, a.reason, a.updated_at,
                       a.tailored_resume_id, j.company, j.title, j.url,
                       j.locations_json, j.source
                FROM applications a JOIN jobs j ON j.id=a.job_id
                WHERE a.status != 'eligible' OR EXISTS
                    (SELECT 1 FROM apply_attempts aa WHERE aa.application_id=a.id)
            """).fetchall()
            attempts: dict[str, list] = {}
            for row in conn.execute("SELECT * FROM apply_attempts ORDER BY started_at DESC, id"):
                attempts.setdefault(row["application_id"], []).append(dict(row))
            resumes: dict[str, list] = {}
            for row in conn.execute("""SELECT id, job_id, pdf_path, created_at
                                       FROM tailored_resumes ORDER BY created_at DESC, id"""):
                resumes.setdefault(row["job_id"], []).append(dict(row))
            has_snapshots = conn.execute("""SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='apply_resume_artifacts'""").fetchone()
            snapshots = {
                row["attempt_id"]: dict(row)
                for row in conn.execute("SELECT * FROM apply_resume_artifacts")
            } if has_snapshots else {}

        result = []
        for row in applications:
            item = dict(row)
            history = attempts.get(item["id"], [])
            successes = [a for a in history if a["status"] == "applied"]
            selected = next(iter(successes or history), None)
            item["status"] = "applied" if successes else (
                history[0]["status"] if history else item["status"]
            )
            item["applied_at"] = successes[0]["finished_at"] if successes else None
            item["last_activity_at"] = (
                history[0]["finished_at"] or history[0]["started_at"]
            ) if history else item["updated_at"]
            item["reason"] = (selected["reason"] if selected else item["reason"]) or ""
            try:
                item["locations"] = json.loads(item.pop("locations_json"))
            except (ValueError, TypeError):
                item["locations"] = []
            item["url"] = item["url"] if urlsplit(item["url"]).scheme in {"http", "https"} else None
            item["attempt_count"] = len(history)
            item["attempts"] = [
                {key: attempt[key] for key in (
                    "id", "status", "reason", "started_at", "finished_at", "turn_count",
                )} for attempt in history
            ]
            artifact = snapshots.get(selected["id"]) if selected else None
            if artifact:
                item["_pdf"] = artifact["pdf_path"]
                item["_sha256"] = artifact["sha256"]
                item["resume_provenance"] = "snapshot"
            else:
                # Best historical record as of the attempt, never a later
                # re-tailoring. Its on-disk file may still have been overwritten.
                candidates = resumes.get(item["job_id"], [])
                legacy = next((r for r in candidates if not selected or
                               r["created_at"] <= selected["started_at"]), None)
                item["_pdf"] = legacy["pdf_path"] if legacy else None
                item["_sha256"] = None
                item["resume_provenance"] = "legacy" if legacy else "missing"
            path = self.pdf_path(item)
            item["resume_available"] = bool(path and path.is_file())
            item["resume_url"] = f"/api/applications/{item['id']}/resume" if item["resume_available"] else None
            result.append(item)
        return sorted(result, key=lambda r: r["applied_at"] or r["last_activity_at"], reverse=True)

    def pdf_path(self, item: dict) -> Path | None:
        if not item.get("_pdf"):
            return None
        path = Path(item["_pdf"])
        path = path if path.is_absolute() else self.root / path
        return path if path.suffix.lower() == ".pdf" else None

    def payload(self) -> dict:
        records = self.records()
        for record in records:
            for key in ("_pdf", "_sha256", "tailored_resume_id", "updated_at"):
                record.pop(key, None)
        today = datetime.now(timezone.utc).date()
        activity = []
        for offset in range(13, -1, -1):
            day = (today - timedelta(days=offset)).isoformat()
            activity.append({"date": day, "count": sum(
                1 for r in records if (r["applied_at"] or "").startswith(day)
            )})
        return {
            "applications": records, "activity": activity,
            "database_exists": self.db.exists(),
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], data: DashboardData):
        self.data = data
        super().__init__(address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, *_args):
        pass  # Do not put application data/URLs in terminal logs.

    def respond(self, status: int, body: bytes, content_type: str, disposition: str | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'self'; frame-src 'self'; base-uri 'none'; frame-ancestors 'self'")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            return self.respond(403, b"Local access only", "text/plain")
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in hosts}:
            return self.respond(403, b"Local access only", "text/plain")
        path = urlsplit(self.path).path
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.css": ("app.css", "text/css; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8")}
        try:
            if path in assets:
                name, kind = assets[path]
                return self.respond(200, files("applyd.dashboard").joinpath("static", name).read_bytes(), kind)
            if path == "/api/applications":
                body = json.dumps(self.server.data.payload(), ensure_ascii=False).encode()
                return self.respond(200, body, "application/json; charset=utf-8")
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "applications"] and parts[3] == "resume":
                record = next((r for r in self.server.data.records() if r["id"] == parts[2]), None)
                pdf = self.server.data.pdf_path(record) if record else None
                if not pdf or not pdf.is_file():
                    return self.respond(404, b"Resume file is unavailable", "text/plain")
                body = pdf.read_bytes()
                if not body.startswith(b"%PDF-"):
                    return self.respond(404, b"Resume file is not a PDF", "text/plain")
                if record["_sha256"] and hashlib.sha256(body).hexdigest() != record["_sha256"]:
                    return self.respond(409, b"Saved resume has changed; original bytes cannot be verified", "text/plain")
                mode = "attachment" if urlsplit(self.path).query == "download=1" else "inline"
                return self.respond(200, body, "application/pdf", f'{mode}; filename="resume.pdf"')
            return self.respond(404, b"Not found", "text/plain")
        except (sqlite3.Error, OSError, ValueError):
            return self.respond(500, b'{"error":"Could not read local history. Check the database and try again."}', "application/json")


def cmd_dashboard(args: argparse.Namespace) -> int:
    load_env()
    db = Path(args.db) if args.db else default_db_path()
    try:
        server = DashboardServer(("127.0.0.1", args.port), DashboardData(db))
    except OSError as exc:
        print(f"Could not start dashboard: {exc}. Try --port 8766.", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"applyd dashboard: {url}\nPress Ctrl+C to stop.", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
    return 0
