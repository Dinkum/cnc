from __future__ import annotations

from dataclasses import dataclass
import json
import re

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.models.entities import Backend
from app.services.placement_config import read_backend_placement
from app.services.validators import ValidationError


INTERFACE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
INTERFACE_STATUS_PENDING = "pending"
INTERFACE_STATUS_ACCEPTED = "accepted"
INTERFACE_STATUS_REJECTED = "rejected"
INTERFACE_STATUSES = {
    INTERFACE_STATUS_PENDING,
    INTERFACE_STATUS_ACCEPTED,
    INTERFACE_STATUS_REJECTED,
}
INTERFACE_DIRECTION_OUT = "out"
INTERFACE_DIRECTION_IN = "in"
INTERFACE_DIRECTION_BIDIRECTIONAL = "bidirectional"
INTERFACE_DIRECTIONS = {
    INTERFACE_DIRECTION_OUT,
    INTERFACE_DIRECTION_IN,
    INTERFACE_DIRECTION_BIDIRECTIONAL,
}


class ConcurrentInterfaceUpdateError(ValidationError):
    pass


@dataclass(frozen=True)
class InterAppInterface:
    name: str
    target_backend_id: int
    status: str = INTERFACE_STATUS_PENDING
    direction: str = INTERFACE_DIRECTION_OUT


@dataclass(frozen=True)
class InboundInterAppInterface:
    name: str
    source_backend_id: int
    source_backend_name: str
    status: str
    direction: str


@dataclass(frozen=True)
class RuntimeInterAppInterface:
    name: str
    source_backend: str
    source_backend_id: int | None
    target_backend: str
    target_backend_id: int | None
    target_handoff_port: int
    network: str
    env_key: str
    url: str
    direction: str = INTERFACE_DIRECTION_OUT


def clean_interface_name(value: object) -> str:
    return str(value or "").strip().lower()


def clean_interface_status(value: object) -> str:
    raw_value = str(value or "").strip().lower()
    aliases = {
        "accept": INTERFACE_STATUS_ACCEPTED,
        "confirm": INTERFACE_STATUS_ACCEPTED,
        "confirmed": INTERFACE_STATUS_ACCEPTED,
        "reject": INTERFACE_STATUS_REJECTED,
    }
    status = aliases.get(raw_value, raw_value)
    return status if status in INTERFACE_STATUSES else INTERFACE_STATUS_PENDING


def clean_interface_direction(value: object) -> str:
    raw_value = str(value or "").strip().lower()
    aliases = {
        "from": INTERFACE_DIRECTION_IN,
        "inbound": INTERFACE_DIRECTION_IN,
        "incoming": INTERFACE_DIRECTION_IN,
        "to": INTERFACE_DIRECTION_OUT,
        "outbound": INTERFACE_DIRECTION_OUT,
        "outgoing": INTERFACE_DIRECTION_OUT,
        "both": INTERFACE_DIRECTION_BIDIRECTIONAL,
        "bi": INTERFACE_DIRECTION_BIDIRECTIONAL,
        "two-way": INTERFACE_DIRECTION_BIDIRECTIONAL,
    }
    direction = aliases.get(raw_value, raw_value)
    return direction if direction in INTERFACE_DIRECTIONS else INTERFACE_DIRECTION_OUT


def interface_network_name(source_backend: str, interface_name: str) -> str:
    return f"cnc-if-{_safe_slug(source_backend)}-{_safe_slug(interface_name)}"


def interface_env_key(interface_name: str) -> str:
    suffix = "".join(
        char.upper() if char.isalnum() else "_"
        for char in clean_interface_name(interface_name)
    ).strip("_")
    return f"CNC_INTERFACE_{suffix}_URL"


def _safe_slug(raw: str) -> str:
    return "".join(
        char if char.isalnum() or char == "-" else "-" for char in raw.lower()
    ).strip("-")


def read_inter_app_interfaces(backend: Backend) -> tuple[InterAppInterface, ...]:
    raw = str(getattr(backend, "inter_app_interfaces_json", "") or "[]").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, list):
        return ()

    interfaces: list[InterAppInterface] = []
    seen_names: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = clean_interface_name(item.get("name"))
        if not name or name in seen_names:
            continue
        try:
            target_backend_id = int(item.get("target_backend_id") or 0)
        except (TypeError, ValueError):
            continue
        if target_backend_id <= 0:
            continue
        interfaces.append(
            InterAppInterface(
                name=name,
                target_backend_id=target_backend_id,
                status=clean_interface_status(item.get("status")),
                direction=clean_interface_direction(item.get("direction")),
            )
        )
        seen_names.add(name)
    return tuple(interfaces)


def inter_app_interfaces_from_form(
    names: list[str],
    target_backend_ids: list[str],
    directions: list[str] | None = None,
) -> tuple[InterAppInterface, ...]:
    interfaces: list[InterAppInterface] = []
    seen_names: set[str] = set()
    for index, raw_name in enumerate(names):
        name = clean_interface_name(raw_name)
        raw_target = (
            target_backend_ids[index] if index < len(target_backend_ids) else ""
        )
        if not name or not str(raw_target or "").strip():
            continue
        try:
            target_backend_id = int(raw_target)
        except (TypeError, ValueError) as exc:
            raise ValidationError("interface target output is invalid") from exc
        if name in seen_names:
            raise ValidationError(f"duplicate interface name: {name}")
        if not INTERFACE_NAME_RE.match(name):
            raise ValidationError(
                "interface names must start with a lowercase letter and use lowercase letters, numbers, or hyphens"
            )
        direction = clean_interface_direction(
            directions[index] if directions and index < len(directions) else ""
        )
        interfaces.append(
            InterAppInterface(
                name=name,
                target_backend_id=target_backend_id,
                direction=direction,
            )
        )
        seen_names.add(name)
    return tuple(interfaces)


def reconcile_inter_app_interface_statuses(
    existing: tuple[InterAppInterface, ...],
    submitted: tuple[InterAppInterface, ...],
) -> tuple[InterAppInterface, ...]:
    """Keep approval state server-side; source output forms cannot self-confirm."""

    existing_by_name = {item.name: item for item in existing}
    reconciled: list[InterAppInterface] = []
    for item in submitted:
        previous = existing_by_name.get(item.name)
        status = (
            previous.status
            if previous
            and previous.target_backend_id == item.target_backend_id
            and previous.direction == item.direction
            else INTERFACE_STATUS_PENDING
        )
        reconciled.append(
            InterAppInterface(
                name=item.name,
                target_backend_id=item.target_backend_id,
                status=status,
                direction=item.direction,
            )
        )
    return tuple(reconciled)


def apply_inter_app_interfaces(
    backend: Backend, interfaces: tuple[InterAppInterface, ...]
) -> None:
    backend.inter_app_interfaces_json = serialize_inter_app_interfaces(interfaces)


def serialize_inter_app_interfaces(
    interfaces: tuple[InterAppInterface, ...],
) -> str:
    return json.dumps(
        [
            {
                "name": item.name,
                "target_backend_id": item.target_backend_id,
                "status": clean_interface_status(item.status),
                "direction": clean_interface_direction(item.direction),
            }
            for item in interfaces
        ],
        separators=(",", ":"),
    )


async def validate_inter_app_interfaces(
    session: AsyncSession,
    *,
    backend_id: int,
    interfaces: tuple[InterAppInterface, ...],
) -> None:
    if not interfaces:
        return
    target_ids = {item.target_backend_id for item in interfaces}
    if backend_id in target_ids:
        raise ValidationError("interface target output must be another app output")

    rows = (
        (
            await session.execute(
                select(Backend).where(Backend.id.in_(sorted(target_ids)))
            )
        )
        .scalars()
        .all()
    )
    targets = {item.id: item for item in rows}
    missing = sorted(target_id for target_id in target_ids if target_id not in targets)
    if missing:
        raise ValidationError(f"unknown interface target output: {missing[0]}")
    for target in targets.values():
        if target.kind != "app":
            raise ValidationError("interface target output must be an app output")


def inbound_inter_app_interfaces(
    backend: Backend,
    all_backends: list[Backend],
) -> tuple[InboundInterAppInterface, ...]:
    inbound: list[InboundInterAppInterface] = []
    if backend.id is None:
        return ()
    for source in sorted(all_backends, key=lambda item: str(item.name)):
        if source.id == backend.id or source.kind != "app":
            continue
        for item in read_inter_app_interfaces(source):
            if item.target_backend_id != backend.id:
                continue
            inbound.append(
                InboundInterAppInterface(
                    name=item.name,
                    source_backend_id=int(source.id),
                    source_backend_name=source.name,
                    status=clean_interface_status(item.status),
                    direction=clean_interface_direction(item.direction),
                )
            )
    return tuple(inbound)


async def apply_inbound_inter_app_interface_statuses(
    session: AsyncSession,
    *,
    target_backend_id: int,
    source_backend_ids: list[str],
    names: list[str],
    statuses: list[str],
    directions: list[str] | None = None,
) -> None:
    updates: dict[tuple[int, str], tuple[str, str | None]] = {}
    for index, raw_source_id in enumerate(source_backend_ids):
        name = clean_interface_name(names[index] if index < len(names) else "")
        if not name:
            continue
        try:
            source_backend_id = int(raw_source_id)
        except (TypeError, ValueError) as exc:
            raise ValidationError("interface source output is invalid") from exc
        status = clean_interface_status(
            statuses[index] if index < len(statuses) else ""
        )
        direction = (
            clean_interface_direction(directions[index])
            if directions and index < len(directions)
            else None
        )
        updates[(source_backend_id, name)] = (status, direction)

    if not updates:
        return

    source_ids = sorted({source_id for source_id, _name in updates})
    sources = (
        (await session.execute(select(Backend).where(Backend.id.in_(source_ids))))
        .scalars()
        .all()
    )
    source_by_id = {int(source.id): source for source in sources}
    missing = sorted(
        source_id for source_id in source_ids if source_id not in source_by_id
    )
    if missing:
        raise ValidationError(f"unknown interface source output: {missing[0]}")

    for source_id, source in source_by_id.items():
        if source.kind != "app":
            raise ValidationError("interface source output must be an app output")
        updated: list[InterAppInterface] = []
        found_names: set[str] = set()
        for item in read_inter_app_interfaces(source):
            next_item = item
            key = (source_id, item.name)
            if item.target_backend_id == target_backend_id and key in updates:
                status, direction = updates[key]
                next_item = InterAppInterface(
                    name=item.name,
                    target_backend_id=item.target_backend_id,
                    status=status,
                    direction=direction or item.direction,
                )
                found_names.add(item.name)
            updated.append(next_item)
        requested_names = {
            name
            for candidate_source_id, name in updates
            if candidate_source_id == source_id
        }
        missing_names = sorted(requested_names - found_names)
        if missing_names:
            raise ValidationError(f"unknown inbound interface: {missing_names[0]}")
        previous_json = str(source.inter_app_interfaces_json or "[]")
        next_json = serialize_inter_app_interfaces(tuple(updated))
        result = await session.execute(
            update(Backend)
            .where(
                Backend.id == source_id,
                Backend.inter_app_interfaces_json == previous_json,
            )
            .values(inter_app_interfaces_json=next_json)
        )
        if result.rowcount != 1:
            raise ConcurrentInterfaceUpdateError(
                "interface configuration changed; refresh and try again"
            )
        set_committed_value(source, "inter_app_interfaces_json", next_json)


def build_runtime_inter_app_interfaces(
    backends: list[Backend],
    *,
    local_backend_names: set[str] | None = None,
) -> tuple[RuntimeInterAppInterface, ...]:
    by_id = {
        int(backend.id): backend
        for backend in backends
        if getattr(backend, "id", None) is not None
    }
    local_names = set(local_backend_names) if local_backend_names is not None else None
    links: list[RuntimeInterAppInterface] = []
    for source in sorted(backends, key=lambda item: str(item.name)):
        if source.kind != "app" or not bool(source.enabled):
            continue
        if not read_backend_placement(source).enabled:
            continue
        if local_names is not None and source.name not in local_names:
            continue
        for item in read_inter_app_interfaces(source):
            if clean_interface_status(item.status) != INTERFACE_STATUS_ACCEPTED:
                continue
            target = by_id.get(item.target_backend_id)
            if target is None or target.kind != "app" or not bool(target.enabled):
                continue
            if not read_backend_placement(target).enabled:
                continue
            if local_names is not None and target.name not in local_names:
                continue
            network = interface_network_name(source.name, item.name)
            env_key = interface_env_key(item.name)
            links.append(
                RuntimeInterAppInterface(
                    name=item.name,
                    source_backend=source.name,
                    source_backend_id=source.id,
                    target_backend=target.name,
                    target_backend_id=target.id,
                    target_handoff_port=int(target.handoff_port),
                    network=network,
                    env_key=env_key,
                    url=f"http://{item.name}:{int(target.handoff_port)}",
                    direction=clean_interface_direction(item.direction),
                )
            )
    return tuple(sorted(links, key=lambda item: (item.source_backend, item.name)))
