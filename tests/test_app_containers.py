from pathlib import Path

from app.config import Settings
from app.models.entities import Backend
from app.services.app_containers import (
    app_debug_tool_commands,
    app_sandbox_profile_state_path,
    configured_app_dns_servers,
    container_ipv4_address,
    network_uses_bridge_dns,
    render_guest_contract_summary,
    runtime_spec,
    write_app_control_assets,
)


def _backend(**overrides: object) -> Backend:
    payload: dict[str, object] = {
        "id": 1,
        "name": "web",
        "kind": "app",
        "sandbox_profile": "ubuntu-24.04-systemd",
        "handoff_port": 8337,
        "volumes_json": "[]",
    }
    payload.update(overrides)
    return Backend(**payload)


def test_render_guest_contract_summary_describes_systemd_guest_boundary() -> None:
    content = render_guest_contract_summary(_backend())

    assert "sandbox profile: ubuntu-24.04-systemd" in content
    assert "seed image: docker.io/library/ubuntu:24.04" in content
    assert "guest init: /sbin/init" in content
    assert "app runtime ownership: guest-local systemd units" in content


def test_debug_toolbelt_includes_sqlite_cli() -> None:
    assert "sqlite3" in app_debug_tool_commands()


def test_container_ipv4_address_reads_backend_private_network() -> None:
    payload = {
        "NetworkSettings": {
            "Networks": {
                "cnc-net-web": {
                    "IPAddress": "10.88.0.8",
                }
            }
        }
    }

    assert container_ipv4_address(_backend(), payload) == "10.88.0.8"


def test_configured_app_dns_servers_prefers_explicit_override(tmp_path: Path) -> None:
    settings = Settings(
        app_container_dns_servers="9.9.9.9, 1.1.1.1, 9.9.9.9",
        app_container_resolv_conf_path=tmp_path / "resolv.conf",
    )

    assert configured_app_dns_servers(settings) == ["9.9.9.9", "1.1.1.1"]


def test_runtime_spec_uses_host_resolvers_without_loopback_stub(tmp_path: Path) -> None:
    resolv_conf = tmp_path / "resolv.conf"
    resolv_conf.write_text(
        "nameserver 127.0.0.53\nnameserver 10.0.0.2\nnameserver 1.1.1.1\n",
        encoding="utf-8",
    )
    settings = Settings(app_container_resolv_conf_path=resolv_conf)

    spec = runtime_spec(_backend(), settings)

    assert spec["sandbox_profile"] == "ubuntu-24.04-systemd"
    assert spec["seed_image"] == "docker.io/library/ubuntu:24.04"
    assert spec["seed_revision"] == 4
    assert spec["dns_servers"] == ["10.0.0.2", "1.1.1.1"]
    assert spec["init_command"] == ["/sbin/init"]


def test_runtime_spec_refreshes_cached_host_resolvers_when_file_changes(
    tmp_path: Path,
) -> None:
    resolv_conf = tmp_path / "resolv.conf"
    resolv_conf.write_text("nameserver 1.1.1.1\n", encoding="utf-8")
    settings = Settings(app_container_resolv_conf_path=resolv_conf)

    first = runtime_spec(_backend(), settings)
    resolv_conf.write_text("nameserver 9.9.9.9\n", encoding="utf-8")

    second = runtime_spec(_backend(), settings)

    assert first["dns_servers"] == ["1.1.1.1"]
    assert second["dns_servers"] == ["9.9.9.9"]


def test_write_app_control_assets_persists_profile_state(tmp_path: Path) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        app_sandbox_dir=tmp_path / "sandboxes",
    )
    backend = _backend()

    write_app_control_assets(backend, settings)

    profile_state = app_sandbox_profile_state_path(settings, backend.name).read_text(
        encoding="utf-8"
    )
    assert "ubuntu-24.04-systemd" in profile_state
    assert "docker.io/library/ubuntu:24.04" in profile_state
    assert '"seed_revision": 4' in profile_state


def test_network_uses_bridge_dns_reads_podman_network_flag() -> None:
    assert network_uses_bridge_dns({"dns_enabled": True}) is True
    assert network_uses_bridge_dns({"dns_enabled": False}) is False
    assert network_uses_bridge_dns(None) is False
