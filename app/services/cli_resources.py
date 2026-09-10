"""Shared CLI resource operations using the same validation and apply path as HTTP."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.database import create_configured_async_engine
from app.models.entities import Backend, Input
from app.schemas.backends import BackendIn, BackendOut, BackendUpdate
from app.schemas.inputs import InputIn, InputOut, InputUpdate
from app.services import backend_commands, input_commands
from app.services.mutation_apply import commit_and_apply
from app.services.operations import host_mutation_operation


@asynccontextmanager
async def resource_session(settings: Settings) -> AsyncIterator[AsyncSession]:
    engine = create_configured_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


def output_payload(item: Backend) -> dict[str, object]:
    return BackendOut.model_validate(item).model_dump(exclude={"shield_code_hash"})


def input_payload(item: Input) -> dict[str, object]:
    data = InputOut.model_validate(item).model_dump(exclude={"shield_code_hash"})
    data["output_ids"] = data.pop("backend_ids")
    return data


async def require_output(session: AsyncSession, name: str) -> Backend:
    item = (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.name == name.strip().lower())
        )
    ).scalar_one_or_none()
    if item is None:
        raise LookupError(f"output not found: {name}")
    return item


async def require_input(session: AsyncSession, reference: str) -> Input:
    condition = (
        Input.id == int(reference)
        if reference.isdecimal()
        else Input.hostname == reference.strip().lower()
    )
    items = (
        (
            await session.execute(
                select(Input).options(selectinload(Input.backends)).where(condition)
            )
        )
        .scalars()
        .all()
    )
    if not items:
        raise LookupError(f"input not found: {reference}")
    if len(items) != 1:
        raise ValueError("input value is ambiguous; use its numeric ID")
    return items[0]


async def read_resources(
    settings: Settings, resource: str, reference: str | None = None
) -> dict[str, object]:
    async with resource_session(settings) as session:
        if resource == "output":
            if reference is not None:
                return {
                    "output": output_payload(await require_output(session, reference))
                }
            items = (
                (
                    await session.execute(
                        select(Backend)
                        .options(selectinload(Backend.inputs))
                        .order_by(Backend.id)
                    )
                )
                .scalars()
                .all()
            )
            return {"outputs": [output_payload(item) for item in items]}
        if reference is not None:
            return {"input": input_payload(await require_input(session, reference))}
        items = (
            (
                await session.execute(
                    select(Input)
                    .options(selectinload(Input.backends))
                    .order_by(Input.id)
                )
            )
            .scalars()
            .all()
        )
        return {"inputs": [input_payload(item) for item in items]}


async def mutate_resource(
    settings: Settings,
    resource: str,
    action: str,
    data: BackendIn | BackendUpdate | InputIn | InputUpdate,
    reference: str | None = None,
) -> dict[str, object]:
    kind = f"cli.{resource}.{action}"
    # Acquire before reading the graph, not merely before applying its replacement.
    async with host_mutation_operation(
        settings, kind=kind, actor="cli", phase="configure"
    ) as operation:
        async with resource_session(settings) as session:
            if resource == "output":
                if action == "create":
                    item = await backend_commands.create_backend(
                        session, data, commit=False
                    )
                else:
                    item = await require_output(session, reference or "")
                    item = await backend_commands.update_backend(
                        session, item.id, data, commit=False
                    )
            else:
                if action == "create":
                    item = await input_commands.create_input(
                        session, data, commit=False
                    )
                else:
                    item = await require_input(session, reference or "")
                    item = await input_commands.update_input(
                        session, item.id, data, commit=False
                    )
            result = await commit_and_apply(
                session, settings, operation=kind, operation_handle=operation
            )
            if not result.applied:
                raise ValueError(
                    result.apply_response.message
                    or "configuration could not be applied"
                )
            if resource == "output":
                item = await backend_commands.require_backend(session, item.id)
                payload = output_payload(item)
            else:
                item = await input_commands.require_input(session, item.id)
                payload = input_payload(item)
            return {resource: payload, "operation_id": operation.id, "changed": True}


def route_payload(item: Input, output: Backend) -> dict[str, object]:
    return {
        "input_id": item.id,
        "input": item.value,
        "input_kind": item.kind,
        "output_id": output.id,
        "output": output.name,
        "enabled": item.enabled and output.enabled,
    }


async def read_routes(settings: Settings) -> dict[str, object]:
    async with resource_session(settings) as session:
        items = (
            (
                await session.execute(
                    select(Input)
                    .options(selectinload(Input.backends))
                    .order_by(Input.id)
                )
            )
            .scalars()
            .all()
        )
        return {
            "routes": [
                route_payload(item, output)
                for item in items
                for output in sorted(item.backends, key=lambda output: output.id)
            ]
        }


async def change_route(
    settings: Settings, input_reference: str, output_name: str, *, connect: bool
) -> dict[str, object]:
    kind = "cli.route.connect" if connect else "cli.route.disconnect"
    async with host_mutation_operation(
        settings, kind=kind, actor="cli", phase="configure"
    ) as operation:
        async with resource_session(settings) as session:
            item = await require_input(session, input_reference)
            output = await require_output(session, output_name)
            ids = set(item.backend_ids)
            changed = (output.id not in ids) if connect else (output.id in ids)
            payload = route_payload(item, output)
            if changed:
                if connect:
                    ids.add(output.id)
                else:
                    ids.discard(output.id)
                await input_commands.update_input(
                    session, item.id, InputUpdate(backend_ids=sorted(ids)), commit=False
                )
                result = await commit_and_apply(
                    session, settings, operation=kind, operation_handle=operation
                )
                if not result.applied:
                    raise ValueError(
                        result.apply_response.message or "route could not be applied"
                    )
            return {
                "route": payload,
                "connected": connect,
                "changed": changed,
                "operation_id": operation.id,
            }
