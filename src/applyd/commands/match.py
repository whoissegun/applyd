from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

from ..config import load_env
from ..local_store import get_local_store
from ..matching import (
    DEFAULT_EMBED_MODEL,
    LocalEmbedder,
    candidate_embedding_text,
    candidate_skill_inventory,
    embedding_source_hash,
    job_embedding_text,
    resume_source_hash,
    score_match,
)


def _load_json(path: str, label: str) -> dict:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"{label} not found: {source}")
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain one JSON object")
    return value


def _normalize(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm == 0:
        raise ValueError("embedding model returned a zero-length vector")
    return vector / norm


def cmd_match(args: argparse.Namespace) -> int:
    """Rank currently eligible jobs with local KNN and deterministic features."""
    load_env()
    store = get_local_store()
    try:
        profile = _load_json(args.profile, "profile")
        resume = _load_json(args.resume, "resume")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    stored_profile = store.get_profile()
    if stored_profile is None:
        print("✗ profile has not been evaluated; run `applyd evaluate`", file=sys.stderr)
        return 2
    stored_value, profile_hash = stored_profile
    if stored_value != profile:
        print(
            "✗ profile.json changed since evaluation; run `applyd evaluate` first",
            file=sys.stderr,
        )
        return 2

    eligible = list(store.iter_eligible_for_matching())
    if not eligible:
        print("✗ no current eligible jobs; run `applyd evaluate` first", file=sys.stderr)
        return 2

    started = time.monotonic()
    print(
        f"→ matching {len(eligible)} eligible jobs locally with {args.model}",
        file=sys.stderr,
    )
    embedder = LocalEmbedder(model=args.model)

    job_texts = {
        job.id: job_embedding_text(job, facts)
        for job, facts, _facts_hash in eligible
    }
    source_hashes = {
        job_id: embedding_source_hash(text, args.model)
        for job_id, text in job_texts.items()
    }
    cached = {} if args.rebuild_embeddings else store.load_job_embeddings(args.model)
    vectors: dict[str, np.ndarray] = {}
    missing: list[tuple[str, str]] = []
    for job, _facts, _facts_hash in eligible:
        row = cached.get(job.id)
        if row and row[0] == source_hashes[job.id]:
            vector = np.frombuffer(row[2], dtype=np.float32)
            if len(vector) == row[1]:
                vectors[job.id] = vector.copy()
                continue
        missing.append((job.id, job_texts[job.id]))

    if missing:
        print(
            f"  → embedding {len(missing)} new or changed jobs",
            file=sys.stderr,
        )
        generated = embedder.passages(text for _job_id, text in missing)
        if len(generated) != len(missing):
            raise RuntimeError(
                f"embedding model returned {len(generated)}/{len(missing)} vectors"
            )
        rows = []
        for (job_id, _text), vector in zip(missing, generated):
            vector = _normalize(vector.astype(np.float32, copy=False))
            vectors[job_id] = vector
            rows.append(
                (
                    job_id,
                    args.model,
                    source_hashes[job_id],
                    int(vector.shape[0]),
                    vector.tobytes(),
                )
            )
        store.save_job_embeddings(rows)

    candidate_text = candidate_embedding_text(resume, profile)
    candidate_vector = _normalize(embedder.query(candidate_text))
    candidate_hash = embedding_source_hash(candidate_text, args.model)
    store.save_candidate_embedding(
        model=args.model,
        source_hash=candidate_hash,
        dimensions=int(candidate_vector.shape[0]),
        embedding=candidate_vector.astype(np.float32).tobytes(),
    )

    jobs = [item[0] for item in eligible]
    matrix = np.stack([_normalize(vectors[job.id]) for job in jobs])
    similarities = matrix @ candidate_vector
    skills = candidate_skill_inventory(resume)
    resume_hash = resume_source_hash(resume)
    records = []
    for (job, facts, _facts_hash), similarity in zip(eligible, similarities):
        result = score_match(
            job,
            facts,
            semantic_similarity=float(similarity),
            candidate_skills=skills,
            profile=profile,
        )
        records.append(
            {
                "job_id": job.id,
                "profile_source_hash": profile_hash,
                "resume_source_hash": resume_hash,
                "job_embedding_source_hash": source_hashes[job.id],
                "embedding_model": args.model,
                "semantic_similarity": result.semantic_similarity,
                "score": result.score,
                "band": result.band,
                "components": result.components,
                "reasons": result.reasons,
            }
        )
    store.replace_match_scores(records)

    ranked = list(
        store.iter_ranked_matches(
            limit=max(1, args.top),
            minimum_score=args.minimum_score,
        )
    )
    if args.format == "json":
        print(json.dumps(ranked, indent=2, ensure_ascii=False))
    else:
        for index, item in enumerate(ranked, 1):
            reasons = "; ".join(item["reasons"][:2]) or "semantic match"
            print(
                f"{index:3d}. {item['score']:5.1f} {item['band']:9s} "
                f"{item['company'][:25]:25s} | {item['title'][:45]:45s} | "
                f"{item['job_id']}\n"
                f"     {reasons}"
            )

    bands = Counter(record["band"] for record in records)
    elapsed = time.monotonic() - started
    print(
        f"✓ matching complete: ranked={len(records)} "
        f"embedded={len(missing)} cached={len(records) - len(missing)} "
        + " ".join(f"{band}={bands[band]}" for band in (
            "excellent", "good", "stretch", "weak"
        ))
        + f" time={elapsed:.2f}s",
        file=sys.stderr,
    )
    return 0
