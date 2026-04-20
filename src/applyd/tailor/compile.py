from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional


def tectonic_available() -> bool:
    return shutil.which("tectonic") is not None


def compile_pdf(
    tex_path: Path,
    outdir: Optional[Path] = None,
    timeout: float = 120.0,
) -> Path:
    """Compile a .tex file to .pdf via tectonic. Returns PDF path.
    Raises RuntimeError if tectonic is missing or compile fails."""
    if not tectonic_available():
        raise RuntimeError(
            "tectonic is not installed. Install with: brew install tectonic"
        )

    tex_path = Path(tex_path)
    outdir = Path(outdir) if outdir else tex_path.parent
    outdir.mkdir(parents=True, exist_ok=True)

    try:
        result = subprocess.run(
            ["tectonic", "--keep-logs", "--outdir", str(outdir), str(tex_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"tectonic timed out after {timeout}s")

    pdf_path = outdir / (tex_path.stem + ".pdf")
    if result.returncode != 0 or not pdf_path.exists():
        stderr_tail = "\n".join(result.stderr.splitlines()[-30:])
        raise RuntimeError(
            f"tectonic compile failed (exit {result.returncode}):\n{stderr_tail}"
        )
    return pdf_path
