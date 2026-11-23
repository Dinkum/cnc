from __future__ import annotations

import os
from pathlib import Path
import tempfile


def replace_bytes(path: Path, content: bytes, *, mode: int, sync: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            if sync:
                os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def replace_bytes_if_changed(path: Path, content: bytes, *, mode: int) -> bool:
    if path.exists() and path.read_bytes() == content:
        return False
    replace_bytes(path, content, mode=mode)
    return True
