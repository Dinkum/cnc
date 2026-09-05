from types import SimpleNamespace

import pytest

from app.services.port_allocator import PortAllocationError, allocate_ports


def backend_stub(backend_id: int, kind: str, port: int | None):
    return SimpleNamespace(id=backend_id, kind=kind, port=port)


def test_allocate_ports_fills_gaps_for_app_backends() -> None:
    backends = [
        backend_stub(1, "app", None),
        backend_stub(2, "app", 12001),
        backend_stub(3, "static", None),
        backend_stub(4, "app", None),
    ]

    allocate_ports(backends, 12000, 12003)

    assert backends[0].port == 12000
    assert backends[1].port == 12001
    assert backends[3].port == 12002


def test_allocate_ports_includes_app_backends() -> None:
    backends = [
        backend_stub(1, "app", None),
        backend_stub(2, "app", 12001),
        backend_stub(3, "app", None),
    ]

    allocate_ports(backends, 12000, 12003)

    assert backends[0].port == 12000
    assert backends[1].port == 12001
    assert backends[2].port == 12002


def test_allocate_ports_raises_when_range_exhausted() -> None:
    backends = [
        backend_stub(1, "app", 12000),
        backend_stub(2, "app", None),
    ]
    with pytest.raises(PortAllocationError):
        allocate_ports(backends, 12000, 12000)


def test_allocate_ports_skips_ports_that_are_not_host_free() -> None:
    backends = [
        backend_stub(1, "app", None),
        backend_stub(2, "app", 12001),
    ]

    allocate_ports(backends, 12000, 12003, is_port_free=lambda port: port != 12000)

    assert backends[0].port == 12002


def test_allocate_ports_skips_reserved_netdata_port() -> None:
    backends = [backend_stub(1, "app", None)]

    allocate_ports(backends, 19999, 20000)

    assert backends[0].port == 20000


def test_allocate_ports_raises_when_host_free_ports_are_exhausted() -> None:
    backends = [backend_stub(1, "app", None)]

    with pytest.raises(PortAllocationError):
        allocate_ports(backends, 12000, 12000, is_port_free=lambda _port: False)
