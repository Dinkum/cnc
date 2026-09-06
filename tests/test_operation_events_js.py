from pathlib import Path
import shutil
import subprocess

import pytest


def test_operation_events_completion_contracts() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the operation watcher tests")
    script = Path(__file__).parent / "js" / "operation-events.test.cjs"
    result = subprocess.run(
        [node, "--test", str(script)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
