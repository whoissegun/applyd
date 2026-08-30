from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..resume_import import convert_resume_tex
from ..tailor import load_resume


def cmd_import_resume(args: argparse.Namespace) -> int:
    source = Path(args.source)
    if source.suffix.casefold() != ".tex":
        raise ValueError("import-resume currently accepts a Jake-style .tex file")
    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    resume = convert_resume_tex(source.read_text(encoding="utf-8"), profile)
    output = Path(args.output)
    output.write_text(json.dumps(resume, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    load_resume(output)
    print(f"wrote {output}: {len(resume['experience'])} experiences, {len(resume['projects'])} projects")
    return 0
