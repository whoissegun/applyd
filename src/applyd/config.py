from __future__ import annotations

import os
from pathlib import Path


def load_env(path: Path = Path(".env")) -> None:
    """Populate os.environ from a .env file, without overwriting existing vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)
