import stat

from app.config import Settings
from app.services.shield_runtime import (
    SHIELD_CONTAINER_SERVICE,
    render_shield_config,
    render_shield_env,
    render_shield_quadlet,
    shield_quadlet_path,
    write_shield_config_asset,
    write_shield_runtime_assets,
)


def _settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        log_path=tmp_path / "app.log",
        app_quadlet_dir=tmp_path / "quadlets",
        shield_state_dir=tmp_path / "shield",
        shield_db_path=tmp_path / "shield" / "shield.db",
        shield_env_file_path=tmp_path / "shield" / "shield.env",
        shield_config_path=tmp_path / "shield" / "shield-config.json",
        shield_secret_key='secret"key',
        shield_access_code_hash="hmac-sha256:v1:abc123",
    )


def test_render_shield_quadlet_uses_loopback_port_and_env_file(tmp_path) -> None:
    settings = _settings(tmp_path)

    content = render_shield_quadlet(settings)

    assert "ContainerName=cnc-shield" in content
    assert "Image=localhost/cnc-shield:current" in content
    assert "PublishPort=127.0.0.1:1026:1026/tcp" in content
    assert f"Volume={settings.shield_state_dir}:/var/lib/cnc/shield:Z" in content
    assert f"EnvironmentFile={settings.shield_env_file_path}" in content
    assert "Environment=SHIELD_DB_PATH=/var/lib/cnc/shield/shield.db" in content
    assert (
        "Environment=SHIELD_CONFIG_PATH=/var/lib/cnc/shield/shield-config.json"
        in content
    )
    assert "Exec=cnc-shield" in content
    assert "--cap-drop=ALL" in content
    assert "--security-opt=no-new-privileges" in content
    assert "Slice=cnc-apps.slice" not in content


def test_render_shield_quadlet_publishes_tailnet_port_for_multi_node(
    monkeypatch, tmp_path
) -> None:
    settings = _settings(tmp_path).model_copy(update={"multi_node_enabled": True})
    monkeypatch.setattr(
        "app.services.shield_runtime.leader_tailnet_ip",
        lambda _settings: "100.64.0.10",
    )

    content = render_shield_quadlet(settings)

    assert "PublishPort=127.0.0.1:1026:1026/tcp" in content
    assert "PublishPort=100.64.0.10:1026:1026/tcp" in content


def test_render_shield_env_writes_bare_values(tmp_path) -> None:
    settings = _settings(tmp_path)

    content = render_shield_env(settings)

    assert 'SHIELD_SECRET_KEY=secret"key\n' in content
    assert "SHIELD_ACCESS_CODE_HASH=hmac-sha256:v1:abc123\n" in content


def test_render_shield_config_maps_output_codes() -> None:
    content = render_shield_config({"app": "hmac-sha256:v1:def456"})

    assert '"app"' in content
    assert '"code_hash": "hmac-sha256:v1:def456"' in content


def test_write_shield_runtime_assets_writes_secret_env_file_private(tmp_path) -> None:
    settings = _settings(tmp_path)

    result = write_shield_runtime_assets(
        settings, output_code_hashes={"app": "hmac-sha256:v1:def456"}
    )

    assert result["container_service"] == SHIELD_CONTAINER_SERVICE
    assert str(shield_quadlet_path(settings)) in result["changed_files"]
    assert str(settings.shield_env_file_path) in result["changed_files"]
    assert str(settings.shield_config_path) in result["changed_files"]
    env_mode = stat.S_IMODE(settings.shield_env_file_path.stat().st_mode)
    quadlet_mode = stat.S_IMODE(shield_quadlet_path(settings).stat().st_mode)
    assert env_mode == 0o600
    assert stat.S_IMODE(settings.shield_config_path.stat().st_mode) == 0o600
    assert quadlet_mode == 0o644


def test_write_shield_config_asset_updates_only_config_file(tmp_path) -> None:
    settings = _settings(tmp_path)
    write_shield_runtime_assets(
        settings, output_code_hashes={"app": "hmac-sha256:v1:old"}
    )

    result = write_shield_config_asset(
        settings, output_code_hashes={"app": "hmac-sha256:v1:new"}
    )

    assert result["changed_files"] == [str(settings.shield_config_path)]
    assert "hmac-sha256:v1:new" in settings.shield_config_path.read_text(
        encoding="utf-8"
    )
    assert str(shield_quadlet_path(settings)) not in result["changed_files"]
    assert str(settings.shield_env_file_path) not in result["changed_files"]
