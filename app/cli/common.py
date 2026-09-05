from __future__ import annotations

import os
from argparse import Namespace
from pathlib import Path
from typing import Awaitable, Callable, TypeAlias

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.services.tailscale_urls import configured_tailnet_admin_host


DEFAULT_ENV_FILE = Path("/etc/cnc.env")
Payload: TypeAlias = dict[str, object]
CommandResult: TypeAlias = tuple[int, Payload | None, str | None]
CommandHandler: TypeAlias = Callable[
    [Namespace, Settings | None], CommandResult | int | Awaitable[CommandResult | int]
]


def load_env_defaults(path: Path = DEFAULT_ENV_FILE) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        normalized = value.strip()
        if (
            len(normalized) >= 2
            and normalized[0] == normalized[-1]
            and normalized[0] in {'"', "'"}
        ):
            normalized = normalized[1:-1]
        os.environ.setdefault(key, normalized)


def resolve_settings() -> Settings:
    from app.config import get_settings

    return get_settings()


def resolve_llm_help_host(settings: Settings) -> str:
    configured = str(getattr(settings, "ssh_advertise_host", "") or "").strip()
    if configured:
        return configured

    ssh_connection = str(os.environ.get("SSH_CONNECTION") or "").split()
    if len(ssh_connection) >= 4:
        connected_host = ssh_connection[2].strip()
        if connected_host:
            return connected_host

    return "SERVER_IP"


def resolve_backend_llm_help_host(settings: Settings) -> str:
    tailnet_host = configured_tailnet_admin_host(settings)
    if tailnet_host:
        return tailnet_host
    return resolve_llm_help_host(settings)


async def load_backend_async(backend_name: str):
    from app.database import SessionLocal
    from app.models.entities import Backend

    async with SessionLocal() as session:
        return (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.name == backend_name)
                .limit(1)
            )
        ).scalar_one_or_none()


async def load_shield_status_payload(settings: Settings) -> dict[str, object]:
    from app.database import SessionLocal
    from app.models.entities import Backend, Input
    from app.services.shield_status import collect_shield_status
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    backends: list[Backend] = []
    inputs: list[Input] = []
    try:
        async with SessionLocal() as session:
            backends = list(
                (
                    await session.execute(
                        select(Backend)
                        .options(selectinload(Backend.inputs))
                        .order_by(Backend.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            inputs = list(
                (
                    await session.execute(
                        select(Input)
                        .options(selectinload(Input.backends))
                        .order_by(Input.id.asc())
                    )
                )
                .scalars()
                .all()
            )
    except Exception:
        # Preserve command output even when shield metadata is temporarily unavailable.
        pass

    return collect_shield_status(backends=backends, inputs=inputs, settings=settings)


def bind_command(
    parser,
    *,
    handler: CommandHandler,
    formatter=None,
    needs_settings: bool = False,
    needs_env: bool = True,
) -> None:
    parser.set_defaults(
        _cli_handler=handler,
        _cli_formatter=formatter,
        _cli_needs_settings=needs_settings,
        _cli_needs_env=needs_env,
    )
