import shutil
import subprocess
from pathlib import Path

import pytest


def test_error_toast_renders_cause_instead_of_final_progress_step():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the dashboard rendering regression")
    source = Path("app/static/js/dashboard.js").read_text()
    renderer = source.split("  const showFloatingSave = ", 1)[1].split(
        "  const showInitialFloatingSaveProgress", 1
    )[0]
    script = (
        """
const assert = require('node:assert/strict');
const floatingSaveShell = {};
const floatingSaveCard = {};
const floatingSaveAction = {};
const floatingSaveStage = {};
const floatingSaveNote = {};
const floatingSaveProgressFill = null;
const floatingSaveMeta = null;
const clearBanners = () => {};
const showFloatingSave = """
        + renderer
        + """
const steps = [{substep: 'Refreshing dashboard'}];
showFloatingSave({tone: 'error', title: 'Input delete failed', headline: 'Review error',
  note: 'Storage validation blocked web. Error CNC-02099-ABCD1234', steps});
assert.equal(floatingSaveNote.textContent, 'Storage validation blocked web. Error CNC-02099-ABCD1234');
showFloatingSave({tone: 'running', title: 'Deleting input', steps});
assert.equal(floatingSaveNote.textContent, 'Refreshing dashboard');
showFloatingSave({tone: 'success', title: 'Input deleted', note: 'Host changes are live.', steps});
assert.equal(floatingSaveNote.textContent, 'Host changes are live.');
"""
    )
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
