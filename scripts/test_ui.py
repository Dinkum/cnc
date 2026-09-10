"""Run the bounded UI regression gate without host services or external requests."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
QUICK_TESTS = (
    "tests/test_metric_ui_js.py",
    "tests/ui_routes/test_dashboard.py",
    "tests/ui_routes/test_view_models.py",
    "tests/ui_routes/test_output_create.py",
    "tests/ui_routes/test_output_update.py",
    "tests/ui_routes/test_output_delete.py",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--unit-only",
        action="store_true",
        help="Skip browser checks explicitly for the shortest edit loop",
    )
    parser.add_argument(
        "--playwright-module", help="Path to an installed Playwright JS entrypoint"
    )
    args = parser.parse_args()
    env = os.environ.copy()
    node = shutil.which("node")
    if not node:
        parser.error("Node.js is required; JavaScript coverage must not silently skip.")
    tests = list(QUICK_TESTS)
    if not args.unit_only:
        config_path = ROOT / "ignore/ui-tests.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        module = (
            args.playwright_module
            or env.get("CNC_PLAYWRIGHT_MODULE")
            or config.get("playwright_module")
        )
        if not module:
            resolved = subprocess.run(
                [node, "-p", "require.resolve('playwright')"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if resolved.returncode == 0:
                module = resolved.stdout.strip()
        if not module or not Path(module).is_file():
            parser.error(
                "Set CNC_PLAYWRIGHT_MODULE or --playwright-module to an installed Playwright JS entrypoint. Use --unit-only only when intentionally skipping the browser."
            )
        env["CNC_PLAYWRIGHT_MODULE"] = str(Path(module).resolve())
        if config.get("browser_channel") and not env.get("CNC_BROWSER_CHANNEL"):
            env["CNC_BROWSER_CHANNEL"] = config["browser_channel"]
        tests.append("tests/test_ui_browser.py")
    started = time.perf_counter()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-s", "--tb=short", *tests],
            cwd=ROOT,
            env=env,
            check=False,
            timeout=60,
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        print("UI regression gate exceeded its 60s hang limit.", file=sys.stderr)
        return 1
    finally:
        scope = (
            "unit/route only; browser skipped"
            if args.unit_only
            else "unit/route + browser"
        )
        print(
            f"UI regression gate ({scope}): {time.perf_counter() - started:.2f}s",
            flush=True,
        )


if __name__ == "__main__":
    raise SystemExit(main())
