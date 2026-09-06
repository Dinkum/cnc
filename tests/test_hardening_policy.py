from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models.entities import Backend, BackendHardeningRun
from app.services import app_hardening
from app.services.app_containers import runtime_spec
from app.services.app_quadlet import render_quadlet_container
from app.services.app_runtime import (
    _normalize_saved_spec,
    _rebuild_required_spec_keys,
    build_app_container_create_command,
)
from app.services.backend_backup_service import (
    _backend_from_metadata,
    _metadata_snapshot,
)
from app.services.hardening_policy import (
    ADVISOR_MANAGED,
    ADVISOR_UNAVAILABLE,
    HardeningPolicy,
    hardening_configuration,
    hardening_podman_args,
    hardening_preview,
    hardening_revision,
    hardening_volumes,
    read_hardening_policy,
    validate_policy_for_backend,
)
from app.services.resource_profile import build_resource_profile


def backend(**overrides) -> Backend:
    return Backend(
        id=1,
        name="web",
        kind="app",
        port=12000,
        handoff_port=8000,
        sandbox_profile="ubuntu-24.04-systemd",
        volumes_json="[]",
        enabled=True,
        **overrides,
    )


def summary(ratings: dict[str, str], **values) -> dict:
    return {
        "phase1": {"id": 1, "status": "success", "details": {"values": values}},
        "phase2": None,
        "features": [
            {
                "setting": key,
                "recommendation": rating,
                "phase1": "likely_safe",
                "phase2": "",
            }
            for key, rating in ratings.items()
        ],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"raw_flags": "--privileged"},
        {"cap_drop_all": "true"},
        {"pids_limit": True},
        {"pids_limit": -1},
        {"nofile_ulimit": 0},
        {"shm_size_mib": 0},
        {"cap_add": ["ALL"], "cap_drop_all": True},
        {"cap_add": ["SYS_ADMIN"]},
        {"cap_add": ["NET_RAW\nExecStart=bad"], "cap_drop_all": True},
        {"apparmor_profile": "unconfined"},
        {"apparmor_profile": "profile%h"},
        {"apparmor_profile": "ok\nPodmanArgs=--privileged"},
        {"ipc": "host"},
        {"ipc": "none", "shm_size_mib": 64},
        {"read_only_tmpfs_false": True},
        {"tmpfs_noexec": True},
    ],
)
def test_policy_rejects_escape_hatches_and_conflicts(payload) -> None:
    with pytest.raises(ValidationError):
        HardeningPolicy.model_validate(payload)


def test_runtime_renderers_enforce_the_same_reviewed_flags(tmp_path: Path) -> None:
    policy = HardeningPolicy(
        cap_drop_all=True,
        cap_add=["CAP_NET_BIND_SERVICE"],
        no_new_privileges=True,
        read_only_rootfs=True,
        read_only_tmpfs_false=True,
        tmpfs_paths="tmp_run",
        tmpfs_noexec=True,
        tmpfs_nosuid=True,
        tmpfs_nodev=True,
        existing_mounts_ro=True,
        existing_mounts_noexec=True,
        existing_mounts_nosuid=True,
        existing_mounts_nodev=True,
        no_hosts_file_if_safe=True,
        pid_private=True,
        ipc="private",
        cgroupns_private=True,
        uts_private=True,
        apparmor_profile="container-default",
        pids_limit=512,
        nofile_ulimit=4096,
        shm_size_mib=64,
    )
    app = backend(hardening_config_json=policy.persisted())
    app.volumes_json = json.dumps(["/srv/data:/data:rw,exec,suid,dev,Z"])
    settings = Settings(
        app_sandbox_dir=tmp_path / "sandboxes", app_container_dns_servers="192.0.2.53"
    )
    profile = build_resource_profile(settings, [app])
    quadlet = render_quadlet_container(app, settings, base_profile=profile)
    command = build_app_container_create_command(app, settings, base_profile=profile)
    for flag in hardening_podman_args(policy):
        assert flag in command
        assert flag in quadlet
    mount = "/srv/data:/data:Z,ro,noexec,nosuid,nodev"
    assert mount in command
    assert f"Volume={mount}" in quadlet
    assert "--privileged" not in quadlet
    assert "PublishPort=127.0.0.1:12000:8000/tcp" in quadlet
    assert runtime_spec(app, settings)["hardening"] == policy.model_dump(
        exclude_defaults=True
    )


def test_missing_runtime_policy_is_an_actual_default_not_the_new_policy() -> None:
    current = {"hardening": {"no_new_privileges": True}, "network": "cnc-net-web"}
    saved = _normalize_saved_spec({"network": "cnc-net-web"}, current)
    assert _rebuild_required_spec_keys(saved, current) == {"hardening"}


def test_mount_flags_preserve_other_options_and_unmodified_mounts() -> None:
    volumes = ["/srv/a:/a:rw,Z", "/srv/b:/b"]
    assert hardening_volumes(HardeningPolicy(), volumes) == volumes
    assert hardening_volumes(HardeningPolicy(existing_mounts_ro=True), volumes) == [
        "/srv/a:/a:Z,ro",
        "/srv/b:/b:ro",
    ]


def test_tmpfs_refuses_to_hide_a_configured_mount() -> None:
    app = backend()
    app.volumes_json = '["/srv/run:/run/app"]'
    with pytest.raises(ValueError, match="overlaps"):
        validate_policy_for_backend(app, HardeningPolicy(tmpfs_paths="run"))


def test_recommendations_use_exact_limits_and_explain_unavailable_controls() -> None:
    app = backend()
    data = summary(
        {
            key: "recommended"
            for key in (
                "pids_limit",
                "nofile_ulimit",
                "shm_size",
                "cap_drop_all",
                "network_none",
                "seccomp_custom_profile",
                "memory_limit",
            )
        },
        pids_limit=768,
        nofile_limit=2048,
        shm_size="96M",
    )
    plan = hardening_preview(app, data, "recommended")
    assert plan["configuration"]["pids_limit"] == 768
    assert plan["configuration"]["nofile_ulimit"] == 2048
    assert "--shm-size=96m" in plan["flags"]
    assert "--cap-drop=ALL" in plan["flags"]
    assert {item["setting"] for item in plan["skipped"]} == {
        "network_none",
        "seccomp_custom_profile",
        "memory_limit",
    }
    assert plan["will_restart"] is True


def test_recommendations_do_not_guess_absent_values_or_override_unsafe_results() -> (
    None
):
    data = summary(
        {
            "pids_limit": "recommended",
            "no_new_privileges": "do not apply",
            "tmpfs_noexec": "recommended",
            "read_only_tmpfs_false": "recommended",
        }
    )
    plan = hardening_preview(backend(), data, "recommended")
    assert plan["flags"] == []
    assert len(plan["skipped"]) == 3


def test_recommendation_ipc_conflicts_are_resolved_explicitly() -> None:
    data = summary(
        {
            "ipc_none": "recommended",
            "ipc_private": "recommended",
            "shm_size": "recommended",
        },
        shm_size="64M",
    )
    plan = hardening_preview(backend(), data, "recommended")
    assert plan["flags"] == ["--ipc=none"]
    assert any(item["setting"] == "shm_size" for item in plan["skipped"])


@pytest.mark.parametrize("phase,status", [("phase1", "running"), ("phase2", "queued")])
def test_active_observation_blocks_configuration_changes(phase, status) -> None:
    data = summary({})
    data[phase] = {"id": 1, "status": status}
    with pytest.raises(ValueError, match="Stop or finish"):
        hardening_preview(backend(), data, "manual", "{}")


def test_incomplete_clone_results_are_not_promoted_to_apply() -> None:
    data = summary({"cap_drop_all": "recommended"})
    data["phase2"] = {"id": 2, "status": "failed"}
    data["features"][0]["phase2"] = "certain_safe"
    plan = hardening_preview(backend(), data, "recommended")
    assert plan["configuration"]["cap_drop_all"] is False
    assert "did not finish" in plan["skipped"][0]["reason"]


def test_revision_changes_with_output_and_evidence() -> None:
    app = backend()
    data = summary({})
    initial = hardening_revision(app, data)
    app.handoff_port = 8080
    assert hardening_revision(app, data) != initial
    app.handoff_port = 8000
    data["phase1"]["id"] = 2
    assert hardening_revision(app, data) != initial


def test_backup_roundtrip_and_previous_configuration_preserve_policy() -> None:
    policy = HardeningPolicy(no_new_privileges=True, pids_limit=512)
    app = backend(
        hardening_config_json=policy.persisted(), hardening_previous_json="{}"
    )
    app.inputs = []
    restored = _backend_from_metadata(_metadata_snapshot(app), backend_name="clone")
    assert read_hardening_policy(restored) == policy
    previous = hardening_preview(app, summary({}), "previous")
    assert previous["flags"] == []
    assert len(previous["changes"]) == 2
    assert hardening_configuration(app, summary({}))["has_previous"] is True


def test_advisor_catalog_has_an_explicit_application_path_or_reason() -> None:
    configurable = set(HardeningPolicy.model_fields)
    translated = {
        "cap_add_exact_exceptions",
        "apparmor_confined",
        "explicit_tmpfs_paths",
        "ipc_none",
        "ipc_private",
        "shm_size",
    }
    assert (
        set(app_hardening.HARDENING_SETTINGS)
        <= configurable
        | translated
        | ADVISOR_MANAGED.keys()
        | ADVISOR_UNAVAILABLE.keys()
    )


def test_old_mount_clone_ratings_are_not_treated_as_enforcement_proof() -> None:
    run = BackendHardeningRun(
        phase="phase2",
        status="success",
        ratings_json='{"existing_mounts_noexec":"certain_safe"}',
        details_json="{}",
    )
    assert app_hardening._feature_rows(None, run) == []
    run.details_json = '{"mount_test_version":1}'
    assert app_hardening._feature_rows(None, run)[0]["phase2"] == "certain_safe"
