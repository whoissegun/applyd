from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..config import load_env
from ..store import JobStore
from ..tailor import (
    TailorClient,
    compile_pdf,
    tectonic_available,
    validate as validate_tailored,
)


def _slugify(text: str, maxlen: int = 60) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in text).lower()
    slug = "-".join(p for p in slug.split("-") if p)
    return slug[:maxlen] or "untitled"


def _strip_fences(text: str) -> str:
    """Strip leading/trailing markdown code fences if the model adds them."""
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
    if text.endswith("```"):
        text = text[: text.rfind("```")].rstrip()
    return text.strip()


def cmd_tailor(args: argparse.Namespace) -> int:
    load_env()
    store = JobStore(Path(args.store))
    store.load()

    job = next((j for j in store.all() if j.id == args.job_id), None)
    if job is None:
        print(f"✗ job not found: {args.job_id}", file=sys.stderr)
        return 1

    if not job.description or len(job.description) < 200:
        print(
            f"✗ job {args.job_id} has no usable description. "
            "Run `applyd enrich` first or pick a different job.",
            file=sys.stderr,
        )
        return 1

    if job.apply_gate and not args.force:
        print(
            f"✗ job {args.job_id} is gated ({job.apply_gate}) — apply agent "
            "can't submit. Skipping tailor to avoid wasted spend. "
            "Use --force to tailor anyway.",
            file=sys.stderr,
        )
        return 1

    base_path = Path(args.base)
    if not base_path.exists():
        print(f"✗ base resume not found: {base_path}", file=sys.stderr)
        return 1
    base_tex = base_path.read_text(encoding="utf-8")

    slug = _slugify(f"{job.company}-{job.title}")
    outdir = Path("out") / slug
    outdir.mkdir(parents=True, exist_ok=True)

    print(
        f"→ tailoring [{job.company}] {job.title} (model: {args.model})...",
        file=sys.stderr,
    )
    try:
        client = TailorClient(model=args.model)
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1

    try:
        tailored, metadata, usage = client.tailor(
            base_resume_tex=base_tex,
            jd_text=job.description,
            company=job.company,
            role=job.title,
        )
    except Exception as e:
        print(f"✗ tailor call failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    tailored = _strip_fences(tailored)

    tex_path = outdir / "resume.tex"
    tex_path.write_text(tailored, encoding="utf-8")
    meta_path = outdir / "metadata.json"
    meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(f"  → wrote {tex_path} ({len(tailored)} chars)", file=sys.stderr)

    cache_c = usage.get("cache_creation_input_tokens", 0)
    cache_r = usage.get("cache_read_input_tokens", 0)
    print(
        f"  tokens: input={usage['input_tokens']} "
        f"output={usage['output_tokens']} "
        f"cache_write={cache_c} cache_read={cache_r}",
        file=sys.stderr,
    )

    # surface metadata — what the model decided
    if metadata:
        if metadata.get("parse_error"):
            print(f"  ⚠ metadata parse error: {metadata['parse_error']}", file=sys.stderr)
        if metadata.get("confidence"):
            print(f"  confidence: {metadata['confidence']}", file=sys.stderr)
        covered = metadata.get("keywords_covered") or []
        missing = metadata.get("keywords_missing") or []
        if covered or missing:
            print(f"  keywords covered ({len(covered)}): {', '.join(covered)}", file=sys.stderr)
            print(f"  keywords missing ({len(missing)}): {', '.join(missing)}", file=sys.stderr)
        for d in metadata.get("decisions_log") or []:
            print(f"  • {d}", file=sys.stderr)
        for r in metadata.get("risk_flags") or []:
            print(f"  ⚠ risk: {r}", file=sys.stderr)

    result = validate_tailored(base_tex, tailored)
    for w in result.warnings:
        print(f"  ⚠ {w}", file=sys.stderr)
    if not result.ok:
        for e in result.errors:
            print(f"  ✗ {e}", file=sys.stderr)
        if not args.ignore_errors:
            print(
                "  skipping PDF compile due to validation errors. "
                "Use --ignore-errors to force.",
                file=sys.stderr,
            )
            return 3

    if args.no_compile:
        print(f"\n✓ tailored .tex ready: {tex_path}", file=sys.stderr)
        return 0

    if not tectonic_available():
        print(
            "  ⚠ tectonic not found on PATH; skipping PDF compile. "
            "Install: brew install tectonic",
            file=sys.stderr,
        )
        return 0

    print("→ compiling PDF with tectonic...", file=sys.stderr)
    try:
        pdf = compile_pdf(tex_path, outdir=outdir)
        print(f"  → wrote {pdf}", file=sys.stderr)
    except RuntimeError as e:
        print(f"  ✗ compile failed: {e}", file=sys.stderr)
        return 4

    job.resume_pdf_path = str(pdf)
    store.save()

    print(f"\n✓ tailor complete: {tex_path} + {pdf}", file=sys.stderr)
    return 0
