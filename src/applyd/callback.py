"""Callback HTTP server for OpenClaw's applyd_apply skill.

The skill POSTs to /apply-result after each job — this endpoint updates
jobs.json so applyd knows what's been applied, skipped, or failed.

Runs on 127.0.0.1 (loopback only). Bearer-token auth via APPLYD_CALLBACK_TOKEN.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .store import JobStore


ApplyStatus = Literal["applied", "skipped", "failed"]


class ApplyResult(BaseModel):
    job_id: str
    status: ApplyStatus
    note: Optional[str] = None


def create_app(store_path: Path) -> FastAPI:
    app = FastAPI(title="applyd callback", version="0.1.0")
    store = JobStore(store_path)
    store.load()
    lock = threading.Lock()
    expected_token = os.environ.get("APPLYD_CALLBACK_TOKEN")

    def _auth(authorization: Optional[str]) -> None:
        if not expected_token:
            return  # no token configured → accept (loopback-only anyway)
        if authorization != f"Bearer {expected_token}":
            raise HTTPException(status_code=401, detail="bad token")

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "jobs": len(store.all())}

    @app.post("/apply-result")
    def apply_result(
        result: ApplyResult,
        authorization: Optional[str] = Header(default=None),
    ) -> dict:
        _auth(authorization)
        with lock:
            # Reload from disk before mutating: applyd tailor / discover may have
            # written between callback startup and now. Without this, save()
            # below would clobber their updates with our stale in-memory copy.
            store.load()
            job = store.get(result.job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"unknown job_id: {result.job_id}")
            store.mark_apply(result.job_id, result.status, result.note)
            # Agent skipped for a structured gate reason → tag apply_gate so
            # future `pending_apply` excludes it and `applyd jobs --gated` surfaces it.
            if result.status == "skipped" and result.note and result.note.startswith("gated:"):
                reason = result.note.split(":", 1)[1].strip() or "unknown"
                job.apply_gate = reason
            store.save()
        return {"ok": True, "job_id": result.job_id, "status": result.status}

    return app
