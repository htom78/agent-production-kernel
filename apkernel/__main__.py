"""Command entry point."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    verify = root / "scripts" / "verify.py"
    if not verify.exists():
        # pip-installed layout has no scripts/; the full gate runs from a repo checkout.
        sys.exit(
            "apkernel is installed as a library; the full verification gate lives in the "
            "repo checkout. Run python3 scripts/verify.py from a clone of "
            "https://github.com/htom78/agent-production-kernel"
        )
    runpy.run_path(str(verify), run_name="__main__")
