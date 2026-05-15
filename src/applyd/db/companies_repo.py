from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from supabase import Client


# Mirrors internal.normalize_company_name() in the SQL migration. Keep in sync.
_SUFFIX_RE = re.compile(
    r"\s+(inc|incorporated|llc|ltd|limited|corp|corporation|company"
    r"|gmbh|s\.a\.|sa|ag|n\.v\.|nv|b\.v\.|bv|plc|kk)\.?$",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[^a-z0-9\s]")
_WS_RE = re.compile(r"\s+")


def _is_unique_violation(exc: BaseException) -> bool:
    """True if exc looks like a Postgres unique-constraint violation (SQLSTATE 23505).

    supabase-py / postgrest wraps the error variously across versions; check
    the most likely surfaces.
    """
    code = getattr(exc, "code", None)
    if code == "23505":
        return True
    message = str(exc)
    return "23505" in message or "duplicate key" in message.lower()


def _normalize_name(name: Optional[str]) -> str:
    if not name:
        return ""
    n = name.strip().lower()
    n = _SUFFIX_RE.sub("", n)
    n = _PUNCT_RE.sub("", n)
    n = _WS_RE.sub(" ", n).strip()
    return n


def _domain_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


class CompaniesRepo:
    """Upsert with layered dedup: alias → ATS slug → domain → normalized name."""

    def __init__(self, client: Client) -> None:
        self.client = client

    def upsert(
        self,
        canonical_name: str,
        ats: Optional[str] = None,
        ats_slug: Optional[str] = None,
        careers_url: Optional[str] = None,
        primary_domain: Optional[str] = None,
    ) -> str:
        name_norm = _normalize_name(canonical_name)
        domain = primary_domain or _domain_from_url(careers_url)

        # 1. manual alias
        if name_norm:
            res = (
                self.client.table("company_aliases")
                .select("company_id")
                .eq("alias_normalized", name_norm)
                .limit(1)
                .execute()
            )
            if res.data:
                return res.data[0]["company_id"]

        # 2. ATS slug overlap
        if ats and ats_slug:
            res = (
                self.client.table("companies")
                .select("id")
                .eq("ats", ats)
                .eq("ats_slug", ats_slug)
                .limit(1)
                .execute()
            )
            if res.data:
                return res.data[0]["id"]

        # 3. domain
        if domain:
            res = (
                self.client.table("companies")
                .select("id")
                .eq("primary_domain", domain)
                .limit(1)
                .execute()
            )
            if res.data:
                return res.data[0]["id"]

        # 4. normalized name
        if name_norm:
            res = (
                self.client.table("companies")
                .select("id")
                .eq("name_normalized", name_norm)
                .limit(1)
                .execute()
            )
            if res.data:
                return res.data[0]["id"]

        # 5. insert — with race-handling. Two parallel discoveries can both
        # reach this branch for the same company; the unique index on
        # name_normalized (or primary_domain) ensures only one wins.
        # On conflict, re-run the cascade — the winning row is now visible.
        payload = {"canonical_name": canonical_name}
        if ats:
            payload["ats"] = ats
        if ats_slug:
            payload["ats_slug"] = ats_slug
        if careers_url:
            payload["careers_url"] = careers_url
        if domain:
            payload["primary_domain"] = domain
        try:
            inserted = self.client.table("companies").insert(payload).execute()
            return inserted.data[0]["id"]
        except Exception as exc:
            if not _is_unique_violation(exc):
                raise
            # Loser of the race — the winner just inserted under one of our
            # dedup keys. Re-run cascade lookups to find their id.
            for col, val in (
                ("name_normalized", name_norm),
                ("primary_domain", domain),
            ):
                if not val:
                    continue
                res = (
                    self.client.table("companies")
                    .select("id")
                    .eq(col, val)
                    .limit(1)
                    .execute()
                )
                if res.data:
                    return res.data[0]["id"]
            raise  # constraint violated but no matching row — shouldn't happen

    def add_alias(self, alias: str, company_id: str) -> None:
        """Map a freeform alias (case-insensitive, after normalization) to a company."""
        alias_norm = _normalize_name(alias)
        if not alias_norm:
            raise ValueError("alias normalizes to empty string")
        self.client.table("company_aliases").upsert(
            {"alias_normalized": alias_norm, "company_id": company_id},
            on_conflict="alias_normalized",
        ).execute()

    def merge(self, loser_id: str, winner_id: str) -> dict:
        """Merge `loser_id` company into `winner_id`. Reassigns jobs and aliases,
        adds the loser's canonical_name as an alias pointing at winner, and
        deletes the loser row.

        Coalesces missing winner fields from loser (so we don't drop info like
        primary_domain or careers_url just because the winner row hadn't been
        populated with them yet). The (ats, ats_slug) pair is single-valued on
        a company — winner keeps its own if it has one; otherwise inherits from
        loser. The other side of the pair is lost (real schema limit).

        Idempotent on re-run: if loser_id no longer exists, returns
        {'status': 'already_merged'} without error.

        Returns: {'status', 'loser_name', 'winner_id', 'jobs_reassigned',
                  'aliases_reassigned', 'backfilled'}
        """
        if loser_id == winner_id:
            raise ValueError("loser_id must differ from winner_id")

        loser_rows = (
            self.client.table("companies").select("*").eq("id", loser_id)
            .limit(1).execute().data
        )
        if not loser_rows:
            return {"status": "already_merged", "winner_id": winner_id}
        loser = loser_rows[0]

        winner_rows = (
            self.client.table("companies").select("*").eq("id", winner_id)
            .limit(1).execute().data
        )
        if not winner_rows:
            raise ValueError(f"winner_id {winner_id} not found")
        winner = winner_rows[0]

        # Coalesce missing winner fields from loser
        backfill = {}
        # (ats, ats_slug) are paired — only inherit both together
        if not winner.get("ats") and loser.get("ats"):
            backfill["ats"] = loser["ats"]
            backfill["ats_slug"] = loser.get("ats_slug")
        for field in ("careers_url", "primary_domain"):
            if not winner.get(field) and loser.get(field):
                backfill[field] = loser[field]
        if backfill:
            (self.client.table("companies").update(backfill)
             .eq("id", winner_id).execute())

        # Reassign aliases pointing at loser — must happen BEFORE delete or
        # CASCADE wipes them.
        aliases_res = (
            self.client.table("company_aliases")
            .update({"company_id": winner_id})
            .eq("company_id", loser_id)
            .execute()
        )
        aliases_reassigned = len(aliases_res.data) if aliases_res.data else 0

        # Reassign jobs
        jobs_res = (
            self.client.table("jobs")
            .update({"company_id": winner_id})
            .eq("company_id", loser_id)
            .execute()
        )
        jobs_reassigned = len(jobs_res.data) if jobs_res.data else 0

        # Add the loser's name as an alias so future upserts of that name
        # short-circuit to winner instead of recreating the row.
        if loser.get("canonical_name"):
            self.add_alias(loser["canonical_name"], winner_id)

        # Delete loser
        self.client.table("companies").delete().eq("id", loser_id).execute()

        return {
            "status": "merged",
            "loser_name": loser.get("canonical_name"),
            "winner_id": winner_id,
            "jobs_reassigned": jobs_reassigned,
            "aliases_reassigned": aliases_reassigned,
            "backfilled": backfill,
        }
