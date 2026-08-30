from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from ..config import load_env
from ..local_store import get_local_store
from ..tailor.compile import compile_pdf
from ..tailor.structured import (
    DEFAULT_MODEL,
    StructuredTailorClient,
    TailorPlanError,
    load_resume,
    pdf_page_count,
    render_latex,
)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:100]


def cmd_tailor(args: argparse.Namespace) -> int:
    load_env()
    store = get_local_store()
    job = store.get(args.job_id)
    if job is None or not job.description:
        print("✗ job missing or has no enriched description", file=sys.stderr)
        return 2

    evaluation = store.get_evaluation(job.id)
    if not args.force and (
        evaluation is None or evaluation.get("decision") != "eligible"
    ):
        decision = (evaluation or {}).get("decision", "not_evaluated")
        print(
            f"✗ job is {decision}; run `applyd evaluate` or pass --force",
            file=sys.stderr,
        )
        return 2

    resume_path = Path(args.resume)
    resume = load_resume(resume_path)
    resume_hash = hashlib.sha256(resume_path.read_bytes()).hexdigest()
    facts = store.get_facts(job.id) or {}
    client = StructuredTailorClient(model=args.model)

    print(f"→ tailoring {job.company} — {job.title} with {args.model}", file=sys.stderr)
    result = client.tailor(
        resume,
        company=job.company,
        role=job.title,
        description=job.description,
        job_facts=facts,
    )
    plan = result.plan
    total_cost = result.cost_usd
    output_dir = Path(args.output) / _slug(f"{job.company}-{job.title}")
    output_dir.mkdir(parents=True, exist_ok=True)
    tex_path = output_dir / "resume.tex"
    pdf_path: Path | None = None

    for attempt in range(3):
        try:
            latex, errors = render_latex(resume, plan, args.template)
        except TailorPlanError as exc:
            errors = [str(exc)]
            latex = ""

        if errors:
            if attempt == 2:
                print("✗ Kimi edit plan still violates layout constraints:", file=sys.stderr)
                for error in errors:
                    print(f"  - {error}", file=sys.stderr)
                return 2
            repair = client.shorten(plan, errors, resume)
            plan = repair.plan
            total_cost += repair.cost_usd
            continue

        tex_path.write_text(latex, encoding="utf-8")
        if args.no_compile:
            break
        try:
            pdf_path = compile_pdf(tex_path, outdir=output_dir)
            pages = pdf_page_count(pdf_path)
        except RuntimeError as exc:
            print(f"✗ PDF compile failed: {exc}", file=sys.stderr)
            return 2
        if pages == 1:
            break
        if attempt == 2:
            if pages == 2:
                print(
                    "⚠ tailored resume remains 2 pages; saving and continuing",
                    file=sys.stderr,
                )
                break
            print(f"✗ tailored resume remains {pages} pages", file=sys.stderr)
            return 2
        repair = client.shorten(
            plan,
            [
                f"Rendered PDF is {pages} pages; shorten bullets and/or select fewer "
                "bullets until the same resume fits exactly one page."
            ],
            resume,
        )
        plan = repair.plan
        total_cost += repair.cost_usd

    (output_dir / "edit_plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tailored_id = store.save_tailored_resume(
        job_id=job.id,
        source_resume_hash=resume_hash,
        model=args.model,
        edit_plan=plan,
        latex_path=str(tex_path.resolve()),
        pdf_path=str(pdf_path.resolve()) if pdf_path else None,
        cost_usd=total_cost,
    )
    print(
        f"✓ tailored resume {tailored_id}: {tex_path} "
        f"PDF={pdf_path or 'not compiled'} Kimi=${total_cost:.4f}",
        file=sys.stderr,
    )
    return 0
