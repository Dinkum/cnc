from pathlib import Path

import pytest

from app.services.file_locks import FileLock


def test_file_lock_reentrant_acquisition_fails_fast_and_releases(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"

    with FileLock(target):
        with pytest.raises(RuntimeError, match="reentrant file lock acquisition"):
            with FileLock(target):
                pass

    with FileLock(target, blocking=False):
        pass
