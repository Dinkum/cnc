import pytest
from pydantic import ValidationError as PydanticValidationError

from app.models.entities import Backend, Input
from app.schemas.backends import BackendUpdate
from app.services.validators import (
    ValidationError,
    ensure_hostname,
    ensure_tailscale_path,
    ensure_tailscale_service,
    validate_backend_collection,
    validate_backend_shape,
    validate_input_bindings,
)


def test_validate_backend_shape_defaults_app_for_mutable_bootstrap() -> None:
    backend = Backend(
        name="worker",
        kind="app",
        base_image="docker.io/library/ubuntu:24.04",
        internal_port=8337,
        workdir="/srv/app",
        install_command="bash init.sh",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json="[]",
        healthcheck_mode=None,
        healthcheck_path="/health",
    )

    validate_backend_shape(backend)

    assert backend.sandbox_profile == "ubuntu-24.04-systemd"
    assert backend.healthcheck_mode is None
    assert backend.healthcheck_path == "/health"
    assert backend.resource_mode == "auto"
    assert backend.resource_size == "small"


def test_validate_backend_shape_does_not_mutate_on_failure() -> None:
    backend = Backend(
        name="worker",
        kind="app",
        sandbox_profile="bad profile",
        handoff_port=8337,
        volumes_json="[]",
        healthcheck_mode=None,
        healthcheck_path="/health",
    )

    with pytest.raises(ValidationError, match="unknown sandbox profile: bad profile"):
        validate_backend_shape(backend)

    assert backend.sandbox_profile == "bad profile"
    assert backend.healthcheck_mode is None
    assert backend.healthcheck_path == "/health"


def test_validate_backend_shape_rejects_removed_http_kind() -> None:
    backend = Backend(
        name="api",
        kind="http",
        internal_port=8000,
        env_json="{}",
        volumes_json="[]",
    )

    try:
        validate_backend_shape(backend)
    except ValidationError as exc:
        assert "invalid kind: http" in str(exc)
        return

    raise AssertionError("expected removed http backend kind to be rejected")


def test_validate_backend_shape_accepts_reserved_shield_backend() -> None:
    backend = Backend(
        name="shield",
        kind="shield",
        port=12000,
        static_root="/srv/should-clear",
        sandbox_profile=None,
        volumes_json="[]",
    )

    validate_backend_shape(backend)

    assert backend.name == "shield"
    assert backend.sandbox_profile == "shield"
    assert backend.port is None
    assert backend.static_root is None


def test_validate_backend_shape_rejects_non_reserved_shield_backend_name() -> None:
    backend = Backend(name="shield-copy", kind="shield", volumes_json="[]")

    with pytest.raises(ValidationError, match="shield backend must be named shield"):
        validate_backend_shape(backend)


@pytest.mark.parametrize("kind", ["app", "static"])
def test_validate_backend_shape_rejects_reserved_shield_name_for_other_kinds(
    kind: str,
) -> None:
    backend = Backend(
        name="shield",
        kind=kind,
        static_root="/srv/docs" if kind == "static" else None,
        volumes_json="[]",
    )

    with pytest.raises(
        ValidationError, match="backend name shield is reserved for the shield output"
    ):
        validate_backend_shape(backend)


def test_validate_backend_shape_rejects_shield_on_static_backend() -> None:
    backend = Backend(
        name="docs",
        kind="static",
        static_root="/srv/docs",
        volumes_json="[]",
        shield_enabled=True,
    )

    with pytest.raises(
        ValidationError, match="shield can only be enabled on app backends"
    ):
        validate_backend_shape(backend)


def test_ensure_hostname_surfaces_specific_label_error() -> None:
    with pytest.raises(
        ValidationError, match="hostname label has invalid characters: bad_label"
    ):
        ensure_hostname("bad_label.example.com")


def test_ensure_hostname_normalizes_url_input() -> None:
    assert ensure_hostname("https://CNC-ADMIN.example.ts.net/path") == (
        "cnc-admin.example.ts.net"
    )
    assert ensure_hostname("web.example.com/health") == "web.example.com"


def test_backend_update_handoff_port_has_bounds() -> None:
    with pytest.raises(PydanticValidationError):
        BackendUpdate(handoff_port=0)


def test_validate_backend_shape_accepts_resource_overrides() -> None:
    backend = Backend(
        name="worker",
        kind="app",
        base_image="docker.io/library/ubuntu:24.04",
        internal_port=8337,
        workdir="/srv/app",
        install_command="bash init.sh",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json="[]",
        resource_mode="manual",
        resource_size="large",
        memory_high_override="512m",
        memory_max_override="1g",
        cpu_quota_override="100",
    )

    validate_backend_shape(backend)

    assert backend.memory_high_override == "512M"
    assert backend.memory_max_override == "1G"
    assert backend.cpu_quota_override == "100%"
    assert backend.resource_mode == "manual"
    assert backend.resource_size == "large"


def test_validate_backend_shape_ignores_legacy_runtime_fields() -> None:
    backend = Backend(
        name="worker",
        kind="app",
        base_image="docker.io/library/ubuntu:24.04",
        internal_port=8337,
        workdir="/srv/app",
        install_command="bash init.sh",
        update_command="git pull --ff-only && .venv/bin/pip install -r requirements.txt",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json="[]",
    )

    validate_backend_shape(backend)

    assert not hasattr(backend, "update_command")
    assert backend.sandbox_profile == "ubuntu-24.04-systemd"
    assert backend.handoff_port == 8337


def test_validate_backend_shape_rejects_static_root_overlapping_cnc_paths() -> None:
    backend = Backend(
        name="docs",
        kind="static",
        static_root="/var/lib/cnc/app-control/site",
        env_json="{}",
        volumes_json="[]",
    )

    with pytest.raises(
        ValidationError, match="static_root for docs cannot overlap protected host path"
    ):
        validate_backend_shape(backend)


def test_validate_backend_shape_rejects_volume_source_overlapping_cnc_paths() -> None:
    backend = Backend(
        name="worker",
        kind="app",
        base_image="docker.io/library/ubuntu:24.04",
        internal_port=8337,
        workdir="/srv/app",
        install_command="bash init.sh",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json='["/var/lib/cnc/backend-backups:/mnt/backups:ro"]',
    )

    with pytest.raises(
        ValidationError,
        match="volume source path for worker cannot overlap protected host path",
    ):
        validate_backend_shape(backend)


def test_validate_backend_shape_rejects_volume_target_shadowing_cnc_control() -> None:
    backend = Backend(
        name="worker",
        kind="app",
        base_image="docker.io/library/ubuntu:24.04",
        internal_port=8337,
        workdir="/srv/app",
        install_command="bash init.sh",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json='["/srv/worker-data:/cnc/control:ro"]',
    )

    with pytest.raises(
        ValidationError,
        match="volume target path for worker cannot overlap cnc-reserved container path /cnc",
    ):
        validate_backend_shape(backend)


def test_validate_backend_shape_accepts_safe_app_volume_bind() -> None:
    backend = Backend(
        name="worker",
        kind="app",
        base_image="docker.io/library/ubuntu:24.04",
        internal_port=8337,
        workdir="/srv/app",
        install_command="bash init.sh",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json='["/srv/worker-data:/srv/app/data:ro"]',
    )

    validate_backend_shape(backend)


def test_validate_backend_shape_rejects_memory_high_above_memory_max() -> None:
    backend = Backend(
        name="worker",
        kind="app",
        base_image="docker.io/library/ubuntu:24.04",
        internal_port=8337,
        workdir="/srv/app",
        install_command="bash init.sh",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json="[]",
        memory_high_override="2G",
        memory_max_override="1G",
    )

    with pytest.raises(ValidationError, match="cannot exceed memory_max_override"):
        validate_backend_shape(backend)


def test_validate_backend_collection_rejects_duplicate_runtime_ports() -> None:
    backends = [
        Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
        ),
        Backend(
            name="smokeapp",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8338,
            workdir="/srv/smokeapp",
            install_command="apt-get update",
            start_command="python3 -m http.server 8338",
            env_json="{}",
            volumes_json="[]",
        ),
    ]

    with pytest.raises(ValidationError, match="duplicate backend port: 12000"):
        validate_backend_collection(backends)


def test_validate_backend_collection_rejects_multiple_shield_backends() -> None:
    backends = [
        Backend(name="shield", kind="shield", volumes_json="[]"),
        Backend(name="shield", kind="shield", volumes_json="[]"),
    ]

    with pytest.raises(ValidationError, match="only one shield backend is allowed"):
        validate_backend_collection(backends)


def test_validate_backend_collection_rejects_health_host_without_matching_domain_input() -> (
    None
):
    backend = Backend(
        name="app-dev",
        kind="app",
        port=12002,
        sandbox_profile="ubuntu-24.04-systemd",
        handoff_port=8337,
        healthcheck_host_header="web.example.com",
    )
    item = Input(kind="tailnet_path", hostname="/app-dev", enabled=True)
    backend.inputs = [item]

    with pytest.raises(
        ValidationError,
        match="healthcheck_host_header for app-dev requires an attached domain input",
    ):
        validate_backend_collection([backend])


def test_validate_backend_collection_accepts_health_host_matching_domain_input() -> (
    None
):
    backend = Backend(
        name="web",
        kind="app",
        port=12001,
        sandbox_profile="ubuntu-24.04-systemd",
        handoff_port=8337,
        healthcheck_host_header="web.example.com",
    )
    item = Input(kind="domain", hostname="web.example.com", enabled=True)
    backend.inputs = [item]

    validate_backend_collection([backend])


def test_validate_backend_collection_rejects_health_host_mismatching_domain_input() -> (
    None
):
    backend = Backend(
        name="web",
        kind="app",
        port=12001,
        sandbox_profile="ubuntu-24.04-systemd",
        handoff_port=8337,
        healthcheck_host_header="other.example.com",
    )
    item = Input(kind="domain", hostname="web.example.com", enabled=True)
    backend.inputs = [item]

    with pytest.raises(
        ValidationError,
        match="healthcheck_host_header for web must match an attached domain input",
    ):
        validate_backend_collection([backend])


def test_validate_input_bindings_rejects_mixed_backend_kinds() -> None:
    app_backend = Backend(
        name="web",
        kind="app",
        base_image="docker.io/library/ubuntu:24.04",
        internal_port=8337,
        workdir="/srv/web",
        install_command="apt-get update",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json="[]",
        enabled=True,
    )
    static_backend = Backend(
        name="docs",
        kind="static",
        static_root="/srv/docs",
        env_json="{}",
        volumes_json="[]",
        enabled=True,
    )
    item = Input(kind="domain", hostname="web.example.com", enabled=True)
    item.backends = [app_backend, static_backend]

    with pytest.raises(ValidationError, match="mixes backend kinds"):
        validate_input_bindings([item])


def test_validate_input_bindings_returns_normalized_binding() -> None:
    app_backend = Backend(
        name="web",
        kind="app",
        port=12000,
        base_image="docker.io/library/ubuntu:24.04",
        internal_port=8337,
        workdir="/srv/web",
        install_command="apt-get update",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json="[]",
        enabled=True,
    )
    item = Input(kind="tailnet_path", hostname="App1", enabled=True)
    item.backends = [app_backend]

    bindings = validate_input_bindings([item])

    assert len(bindings) == 1
    assert bindings[0].input_kind == "tailnet_path"
    assert bindings[0].input_value == "/app1"
    assert bindings[0].attached_enabled_backends == [app_backend]


def test_validate_input_bindings_returns_tailnet_service_binding() -> None:
    app_backend = Backend(
        name="app-dev",
        kind="app",
        port=12002,
        base_image="docker.io/library/ubuntu:24.04",
        internal_port=8337,
        workdir="/srv/web",
        install_command="apt-get update",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json="[]",
        enabled=True,
    )
    item = Input(kind="tailnet_service", hostname="Svc:App-Dev", enabled=True)
    item.backends = [app_backend]

    bindings = validate_input_bindings([item])

    assert len(bindings) == 1
    assert bindings[0].input_kind == "tailnet_service"
    assert bindings[0].input_value == "app-dev"
    assert bindings[0].attached_enabled_backends == [app_backend]


def test_ensure_tailscale_path_rejects_shield_reserved_paths() -> None:
    with pytest.raises(
        ValidationError, match="tailnet path is reserved for system routing"
    ):
        ensure_tailscale_path("/shield/access")
    with pytest.raises(
        ValidationError, match="tailnet path is reserved for system routing"
    ):
        ensure_tailscale_path("/shield/check")


def test_ensure_tailscale_service_rejects_netdata_reserved_name() -> None:
    with pytest.raises(ValidationError, match="tailnet service name is reserved"):
        ensure_tailscale_service("netdata")


def test_validate_backend_shape_rejects_reserved_netdata_port() -> None:
    backend = Backend(
        name="web",
        kind="app",
        port=19999,
        sandbox_profile="ubuntu-24.04-systemd",
        handoff_port=8337,
        volumes_json="[]",
    )

    with pytest.raises(ValidationError, match="reserved host port"):
        validate_backend_shape(backend)


def test_validate_input_bindings_returns_shield_binding() -> None:
    shield_backend = Backend(
        name="shield", kind="shield", enabled=True, volumes_json="[]"
    )
    item = Input(kind="shield", hostname="shield.example.com", enabled=True)
    item.backends = [shield_backend]

    bindings = validate_input_bindings([item])

    assert len(bindings) == 1
    assert bindings[0].input_kind == "shield"
    assert bindings[0].input_value == "shield.example.com"
    assert bindings[0].attached_enabled_backends == [shield_backend]


def test_validate_input_bindings_accepts_domain_input_shield() -> None:
    app_backend = Backend(name="web", kind="app", enabled=True, volumes_json="[]")
    item = Input(
        kind="domain",
        hostname="web.example.com",
        enabled=True,
        shield_enabled=True,
        shield_code_hash="hmac-sha256:v1:abc",
    )
    item.backends = [app_backend]

    bindings = validate_input_bindings([item])

    assert len(bindings) == 1


def test_validate_input_bindings_rejects_input_shield_without_code() -> None:
    app_backend = Backend(name="web", kind="app", enabled=True, volumes_json="[]")
    item = Input(
        kind="domain", hostname="web.example.com", enabled=True, shield_enabled=True
    )
    item.backends = [app_backend]

    with pytest.raises(ValidationError, match="shielded input requires an access code"):
        validate_input_bindings([item])


def test_validate_input_bindings_rejects_input_shield_on_non_domain_input() -> None:
    app_backend = Backend(name="web", kind="app", enabled=True, volumes_json="[]")
    item = Input(
        kind="tailnet_path",
        hostname="/web",
        enabled=True,
        shield_enabled=True,
        shield_code_hash="hmac-sha256:v1:abc",
    )
    item.backends = [app_backend]

    with pytest.raises(
        ValidationError, match="shield can only be enabled on domain inputs"
    ):
        validate_input_bindings([item])


def test_validate_input_bindings_rejects_shield_output_on_tailnet_input() -> None:
    shield_backend = Backend(
        name="shield", kind="shield", enabled=True, volumes_json="[]"
    )
    item = Input(kind="tailnet_path", hostname="/shield-output", enabled=True)
    item.backends = [shield_backend]

    with pytest.raises(
        ValidationError, match="shield output must be attached to a shield input"
    ):
        validate_input_bindings([item])


def test_validate_input_bindings_rejects_tailnet_service_multi_output() -> None:
    first = Backend(
        name="web",
        kind="app",
        port=12001,
        base_image="docker.io/library/ubuntu:24.04",
        internal_port=8337,
        workdir="/srv/web",
        install_command="apt-get update",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json="[]",
        enabled=True,
    )
    second = Backend(
        name="app-dev",
        kind="app",
        port=12002,
        base_image="docker.io/library/ubuntu:24.04",
        internal_port=8337,
        workdir="/srv/web",
        install_command="apt-get update",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json="[]",
        enabled=True,
    )
    item = Input(kind="tailnet_service", hostname="app-dev", enabled=True)
    item.backends = [first, second]

    with pytest.raises(
        ValidationError,
        match="tailnet service app-dev must attach exactly one enabled output",
    ):
        validate_input_bindings([item])
