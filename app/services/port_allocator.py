from collections.abc import Callable

from app.models.entities import Backend
from app.services.netdata_constants import NETDATA_PORT
from app.services.port_preflight import is_loopback_port_free


class PortAllocationError(Exception):
    pass


RESERVED_HOST_PORTS = frozenset({NETDATA_PORT})


def allocate_ports(
    backends: list[Backend],
    range_start: int,
    range_end: int,
    *,
    is_port_free: Callable[[int], bool] | None = None,
) -> list[Backend]:
    if range_end < range_start:
        raise PortAllocationError("invalid port range")
    port_is_free = is_port_free or is_loopback_port_free

    used_ports = {
        backend.port
        for backend in backends
        if backend.kind == "app" and backend.port is not None
    } | RESERVED_HOST_PORTS
    cursor = range_start

    for backend in sorted(backends, key=lambda item: item.id):
        if backend.kind != "app":
            continue
        if backend.port is not None:
            continue
        while cursor <= range_end and (
            cursor in used_ports or not port_is_free(cursor)
        ):
            cursor += 1
        if cursor > range_end:
            raise PortAllocationError(
                f"no free ports in range {range_start}-{range_end}"
            )
        backend.port = cursor
        used_ports.add(cursor)
        cursor += 1

    return backends
