#!/usr/bin/env python3
"""Generate or check the hashed deployment manifest from the project lock."""

import argparse
from pathlib import Path
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--offline",
            "--no-dev",
            "--no-emit-project",
            "--no-header",
            "--no-annotate",
        ],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )
    path = root / "requirements.txt"
    if args.check:
        if path.read_text() != result.stdout:
            raise SystemExit(
                "requirements.txt differs from uv.lock; run scripts/sync_requirements.py"
            )
        print("Deployment requirements match uv.lock.")
    else:
        path.write_text(result.stdout)


if __name__ == "__main__":
    main()
