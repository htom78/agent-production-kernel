"""Command entry point."""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    runpy.run_path(str(root / "scripts" / "verify.py"), run_name="__main__")
