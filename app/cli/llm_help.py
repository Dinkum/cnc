from __future__ import annotations

import argparse

from app.cli.common import (
    CommandResult,
    bind_command,
    resolve_backend_llm_help_host,
    resolve_llm_help_host,
)
from app.config import Settings


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    llm_help = subparsers.add_parser("llm-help")
    llm_help.add_argument("backend", nargs="?", default=None)
    llm_help.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        llm_help,
        handler=_run_llm_help_command,
        formatter=render_llm_help_text,
        needs_settings=True,
    )


def render_llm_help_text(payload: dict[str, object]) -> str:
    from app.services.llm_help import (
        render_backend_llm_help_text,
        render_host_llm_help_text,
    )

    if isinstance(payload.get("cnc"), dict):
        return render_host_llm_help_text(payload)
    return render_backend_llm_help_text(payload)


async def llm_help_async(
    backend_name: str | None,
    settings: Settings,
) -> CommandResult:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.database import SessionLocal
    from app.models.entities import Backend
    from app.services.llm_help import (
        backend_status_context,
        build_backend_llm_help_payload,
        build_host_llm_help_payload,
        cached_service_map,
    )
    from app.services.renderers import container_name
    from app.services.status_service import peek_cached_status

    async with SessionLocal() as session:
        backends = list(
            (
                await session.execute(
                    select(Backend)
                    .options(selectinload(Backend.inputs))
                    .order_by(Backend.id.asc())
                )
            ).scalars()
        )
        backend = None
        if isinstance(backend_name, str):
            backend = next(
                (item for item in backends if item.name == backend_name), None
            )
            if backend is None:
                return 1, None, f"backend not found: {backend_name}"

        # LLM help must remain available when a guest or the wider status fan-out is
        # unhealthy. Cached status is context, not a dependency of the handoff.
        service_map = cached_service_map(peek_cached_status())
        llm_help_host = resolve_llm_help_host(settings)
        backend_ssh_host = resolve_backend_llm_help_host(settings)

        if backend is not None:
            service_payload = service_map.get(container_name(backend.name))
            status_value, service_state, runtime_diagnosis, runtime_issues = (
                backend_status_context(backend, service_payload)
            )
            payload = build_backend_llm_help_payload(
                backend,
                host=llm_help_host,
                backend_ssh_host=backend_ssh_host,
                status_value=status_value,
                service_state=service_state,
                runtime_diagnosis=runtime_diagnosis,
                runtime_issues=runtime_issues,
            )
            return 0, payload, None

        backend_payloads: list[dict[str, object]] = []
        for item in backends:
            service_payload = service_map.get(container_name(item.name))
            status_value, service_state, runtime_diagnosis, runtime_issues = (
                backend_status_context(item, service_payload)
            )
            backend_payloads.append(
                build_backend_llm_help_payload(
                    item,
                    host=llm_help_host,
                    backend_ssh_host=backend_ssh_host,
                    status_value=status_value,
                    service_state=service_state,
                    runtime_diagnosis=runtime_diagnosis,
                    runtime_issues=runtime_issues,
                )
            )
        payload = build_host_llm_help_payload(
            backends,
            settings=settings,
            host=llm_help_host,
            backend_ssh_host=backend_ssh_host,
            backend_payloads=backend_payloads,
        )
        return 0, payload, None


async def _run_llm_help_command(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    assert settings is not None
    return await llm_help_async(args.backend, settings)
