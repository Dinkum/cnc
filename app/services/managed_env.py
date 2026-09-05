from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

from app.config import Settings, reload_settings
from app.services.host_state import (
    LockedStateTransaction,
    host_state_lock_path,
    write_text_atomic,
)


_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SAFE_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9._:/@%+=,-]+$")


def managed_env_lock_path(path: Path) -> Path:
    return host_state_lock_path(path)


def _normalize_env_updates(updates: Mapping[str, str | None]) -> dict[str, str | None]:
    normalized: dict[str, str | None] = {}
    for key, value in updates.items():
        if not _ENV_KEY_RE.fullmatch(key):
            msg = f"invalid environment key: {key!r}"
            raise ValueError(msg)
        cleaned = value.strip() if isinstance(value, str) else None
        normalized[key] = cleaned or None
    return normalized


def _read_managed_env_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _write_managed_env_text(path: Path, content: str) -> None:
    write_text_atomic(path, content, encoding="utf-8")


def _update_managed_env_file_unlocked(
    existing_text: str, normalized: Mapping[str, str | None]
) -> str:
    existing_lines = existing_text.splitlines()
    rendered: list[str] = []
    consumed: set[str] = set()

    for raw_line in existing_lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            rendered.append(raw_line)
            continue
        key, _, _value = raw_line.partition("=")
        env_key = key.strip()
        if env_key not in normalized:
            rendered.append(raw_line)
            continue
        consumed.add(env_key)
        replacement = normalized[env_key]
        if replacement is None:
            continue
        rendered.append(f"{env_key}={_format_env_value(replacement)}")

    for key, value in normalized.items():
        if key in consumed or value is None:
            continue
        rendered.append(f"{key}={_format_env_value(value)}")

    content = "\n".join(rendered).rstrip()
    return f"{content}\n" if content else ""


def update_managed_env_file(path: Path, updates: Mapping[str, str | None]) -> None:
    normalized = _normalize_env_updates(updates)
    with LockedStateTransaction[str](
        path,
        loader=_read_managed_env_text,
        writer=_write_managed_env_text,
    ) as transaction:
        transaction.replace(
            _update_managed_env_file_unlocked(transaction.value, normalized)
        )


def apply_managed_env_updates(
    settings: Settings, updates: Mapping[str, str | None]
) -> Settings:
    normalized = _normalize_env_updates(updates)
    with LockedStateTransaction[str](
        settings.managed_env_file_path,
        loader=_read_managed_env_text,
        writer=_write_managed_env_text,
    ) as transaction:
        transaction.replace(
            _update_managed_env_file_unlocked(transaction.value, normalized)
        )
    for key, value in normalized.items():
        if isinstance(value, str):
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    return reload_settings()


def _format_env_value(value: str) -> str:
    if _SAFE_ENV_VALUE_RE.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
