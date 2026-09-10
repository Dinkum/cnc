from pathlib import Path
import shutil
import subprocess

import pytest


def test_metric_ui_helpers():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for UI helper tests")
    result = subprocess.run(
        [
            node,
            "--test",
            *[
                str(Path(__file__).parent / "js" / name)
                for name in (
                    "metric-ui.test.cjs",
                    "dashboard-tabs.test.cjs",
                    "request-deadline.test.cjs",
                )
            ],
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
