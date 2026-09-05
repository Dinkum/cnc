from app.config import Settings
from app.models.entities import Backend
from app.services.resource_profile import (
    RESOURCE_SIZE_WEIGHTS,
    backend_resource_profile,
    build_resource_profile,
)


def _app_backend(
    name: str, *, resource_mode: str = "auto", resource_size: str = "small"
) -> Backend:
    return Backend(
        name=name,
        kind="app",
        base_image="docker.io/library/ubuntu:24.04",
        internal_port=8337,
        workdir=f"/srv/{name}",
        install_command="apt-get update",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json="[]",
        resource_mode=resource_mode,
        resource_size=resource_size,
    )


def test_resource_profile_auto_mode_uses_host_relative_size_mix(monkeypatch) -> None:
    settings = Settings(
        auto_resource_limits=True,
        auto_memory_reserve_percent=20,
        auto_cpu_reserve_percent=20,
        auto_min_memory_high_mb=128,
        auto_min_cpu_quota_percent=25,
        auto_cpu_burst_factor=2.0,
    )
    monkeypatch.setattr("app.services.resource_profile.os.cpu_count", lambda: 4)
    monkeypatch.setattr(
        "app.services.resource_profile._detect_total_memory_bytes",
        lambda: 10 * 1024 * 1024 * 1024,
    )
    backends = [
        _app_backend("a", resource_size="small"),
        _app_backend("b", resource_size="small"),
        _app_backend("c", resource_size="small"),
        _app_backend("d", resource_size="medium"),
        _app_backend("e", resource_size="large"),
    ]

    profile = build_resource_profile(settings, backends)
    large_profile = backend_resource_profile(backends[-1], profile)
    medium_profile = backend_resource_profile(backends[-2], profile)
    small_profile = backend_resource_profile(backends[0], profile)

    assert profile.mode == "auto"
    assert profile.size_counts == {"small": 3, "medium": 1, "large": 1}
    assert profile.total_size_weight == 9
    assert profile.app_memory_budget_bytes == 8 * 1024 * 1024 * 1024
    assert profile.app_cpu_budget_percent == 80
    assert small_profile.memory_high == "910M"
    assert medium_profile.memory_high == "1820M"
    assert large_profile.memory_high == "3640M"
    assert small_profile.memory_max == "1365M"
    assert medium_profile.memory_max == "2730M"
    assert large_profile.memory_max == "5461M"
    assert small_profile.cpu_quota == "25%"
    assert medium_profile.cpu_quota == "36%"
    assert large_profile.cpu_quota == "72%"
    assert small_profile.cpu_shares == 1024 * RESOURCE_SIZE_WEIGHTS["small"]
    assert large_profile.cpu_entitlement_percent_of_host == 35.55555555555556


def test_resource_profile_static_fallback(monkeypatch) -> None:
    settings = Settings(
        default_memory_high="300M",
        default_memory_max="420M",
        default_cpu_quota="60%",
    )
    monkeypatch.setattr("app.services.resource_profile.os.cpu_count", lambda: None)
    monkeypatch.setattr(
        "app.services.resource_profile._detect_total_memory_bytes", lambda: None
    )

    profile = build_resource_profile(settings, [_app_backend("worker")])

    assert profile.mode == "static"
    assert profile.memory_high == "300M"
    assert profile.memory_max == "420M"
    assert profile.cpu_quota == "60%"
    assert profile.reason == "host resources unavailable"


def test_backend_resource_profile_manual_mode_pins_size_but_keeps_overrides() -> None:
    settings = Settings(
        auto_resource_limits=True,
        auto_memory_reserve_percent=20,
        auto_cpu_reserve_percent=20,
        auto_cpu_burst_factor=2.0,
    )
    base_backends = [
        _app_backend("tiny", resource_size="small"),
        _app_backend("big", resource_size="large"),
    ]
    base_profile = build_resource_profile(settings, base_backends)
    backend = _app_backend("manual", resource_mode="manual", resource_size="large")
    backend.memory_high_override = "768M"
    backend.memory_max_override = "1.5G"
    backend.cpu_quota_override = "100%"

    profile = backend_resource_profile(backend, base_profile)

    assert profile.mode == "manual"
    assert profile.resource_size == "large"
    assert profile.memory_high == "768M"
    assert profile.memory_max == "1.5G"
    assert profile.cpu_quota == "100%"
    assert profile.cpu_shares == 4096
