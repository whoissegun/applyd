"""Backward-compatible wrapper for `applyd import-resume`."""
from __future__ import annotations

import argparse

from applyd.commands.import_resume import cmd_import_resume


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", nargs="?", default="resume_base.tex")
    parser.add_argument("--tex", dest="source_flag")
    parser.add_argument("--profile", default="profile.json")
    parser.add_argument("--output", default="resume.json")
    args = parser.parse_args()
    args.source = args.source_flag or args.source
    return cmd_import_resume(args)


if __name__ == "__main__":
    raise SystemExit(main())
