from itertools import chain

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.entities import Backend
from app.services.port_allocator import RESERVED_HOST_PORTS
from app.services.port_preflight import is_loopback_port_free
from app.services.validators import ensure_backend_name


async def next_clone_name(session: AsyncSession, source_name: str) -> str:
    existing_names = set((await session.execute(select(Backend.name))).scalars())
    counter = 1
    while True:
        prefix = "clone-"
        suffix = "" if counter == 1 else f"-{counter}"
        trimmed = source_name[: 63 - len(prefix) - len(suffix)].rstrip("-") or "backend"
        candidate = ensure_backend_name(f"{prefix}{trimmed}{suffix}")
        if candidate not in existing_names:
            return candidate
        counter += 1


async def next_clone_port(
    session: AsyncSession, source_port: int | None, settings: Settings
) -> int | None:
    used_ports = (
        set(
            (
                await session.execute(
                    select(Backend.port).where(
                        Backend.kind == "app", Backend.port.is_not(None)
                    )
                )
            ).scalars()
        )
        | RESERVED_HOST_PORTS
    )
    start, end = settings.port_range_start, settings.port_range_end
    first = max(start, source_port + 1) if source_port is not None else start
    first = min(first, end + 1)
    # A suggested port is not a reservation; mutation-time preflight still owns
    # the final availability check. Prefer the next port, then reuse lower gaps.
    for port in chain(range(first, end + 1), range(start, first)):
        if port not in used_ports and is_loopback_port_free(port):
            return port
    return None
