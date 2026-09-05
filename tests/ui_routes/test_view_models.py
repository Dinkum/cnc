from .support import (
    Backend,
    DEFAULT_UI_APP_SANDBOX_PROFILE,
    _normalize_backend_form_sandbox_image,
    datetime,
    timedelta,
    timezone,
    ui_output,
    ui_shared,
    view_models,
)


def test_output_load_meter_reports_active_percent() -> None:
    payload = ui_shared._output_load_meter(
        18.4, enabled=True, kind="app", service_state="active"
    )

    assert payload == {
        "label": "18.4%",
        "percent": 18.4,
        "tone": "active",
        "available": True,
        "compact": False,
    }


def test_output_load_meter_reports_collecting_for_active_app_without_metrics() -> None:
    payload = ui_shared._output_load_meter(
        None, enabled=True, kind="app", service_state="active"
    )

    assert payload == {
        "label": "collecting",
        "percent": 0.0,
        "tone": "queued",
        "available": False,
        "compact": False,
    }


def test_output_load_meter_reports_shield_metrics_like_managed_runtime() -> None:
    payload = ui_shared._output_load_meter(
        12.5, enabled=True, kind="shield", service_state="active"
    )

    assert payload == {
        "label": "12.5%",
        "percent": 12.5,
        "tone": "active",
        "available": True,
        "compact": False,
    }


def test_service_metrics_lookup_uses_shield_service_name() -> None:
    backend = Backend(name="shield", kind="shield")
    metrics = {"cpu_percent": 2.5, "memory_percent": 12.5}

    payload = ui_shared._service_metrics_from_status_payload(
        {"services": [{"service": "cnc-shield.service", "metrics": metrics}]},
        backend,
    )

    assert payload == metrics


def test_runtime_service_name_uses_kind_not_reserved_name_alone() -> None:
    assert (
        ui_shared._backend_runtime_service_name("shield", kind="shield")
        == "cnc-shield.service"
    )
    assert (
        ui_shared._backend_runtime_service_name("shield", kind="app")
        == "cnc-app-shield"
    )


def test_network_isolation_home_status_labels_security_state() -> None:
    assert ui_shared._network_isolation_home_status(
        {"app_network_isolation": {"checked": True, "ok": True}}
    ) == {"label": "OK", "tone": "success"}
    assert ui_shared._network_isolation_home_status(
        {
            "app_network_isolation": {
                "checked": True,
                "ok": False,
                "leaks": [{"source": "a", "target": "b"}],
            }
        }
    ) == {"label": "warning", "tone": "warn"}
    assert ui_shared._network_isolation_home_status({}) == {
        "label": "unknown",
        "tone": "warn",
    }


def test_cluster_node_state_tones_are_semantic() -> None:
    assert ui_shared._cluster_node_state_tone("healthy") == "healthy"
    assert ui_shared._cluster_node_state_tone("joining") == "pending"
    assert ui_shared._cluster_node_state_tone("degraded") == "degraded"
    assert ui_shared._cluster_node_state_tone("removed") == "inactive"


def test_output_runtime_badge_marks_shield_unknown_without_cached_service_snapshot() -> (
    None
):
    payload = ui_shared._output_runtime_badge(
        enabled=True,
        kind="shield",
        runtime_diagnostics=None,
        unit_data={},
        unit_ok=False,
    )

    assert payload == {"value": "unknown", "tone": "queued"}


def test_output_runtime_badge_marks_explicit_failed_shield_service_unhealthy() -> None:
    payload = ui_shared._output_runtime_badge(
        enabled=True,
        kind="shield",
        runtime_diagnostics=None,
        unit_data={"ActiveState": "failed"},
        unit_ok=False,
    )

    assert payload == {"value": "unhealthy", "tone": "error"}


def test_output_runtime_badge_marks_nonactive_shield_service_unhealthy() -> None:
    payload = ui_shared._output_runtime_badge(
        enabled=True,
        kind="shield",
        runtime_diagnostics=None,
        unit_data={"ActiveState": "failed"},
        unit_ok=True,
    )

    assert payload == {"value": "unhealthy", "tone": "error"}


def test_output_runtime_badge_marks_deferred_guest_observation_unknown() -> None:
    payload = ui_shared._output_runtime_badge(
        enabled=True,
        kind="app",
        runtime_diagnostics={"diagnosis": "backend_observation_deferred"},
        unit_data={"ActiveState": "active"},
        unit_ok=False,
    )

    assert payload == {"value": "unknown", "tone": "queued"}


def test_output_load_meter_reports_off_for_disabled_backend() -> None:
    payload = ui_shared._output_load_meter(
        42.0, enabled=False, kind="app", service_state="active"
    )

    assert payload == {
        "label": "OFF",
        "percent": 0.0,
        "tone": "inactive",
        "available": False,
        "compact": False,
    }


def test_output_load_meter_reports_compact_inactive_app_without_metrics() -> None:
    payload = ui_shared._output_load_meter(
        None, enabled=True, kind="app", service_state="inactive"
    )

    assert payload == {
        "label": "",
        "percent": 0.0,
        "tone": "inactive",
        "available": False,
        "compact": True,
    }


def test_change_state_marks_recent_unapplied_create_as_new() -> None:
    now = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
    created_at = now - timedelta(hours=6)

    assert (
        ui_shared._change_state(
            created_at=created_at, updated_at=None, last_applied_at=None, now=now
        )
        == "new"
    )


def test_format_timestamp_uses_human_readable_fallback_without_timezone() -> None:
    value = datetime(2026, 4, 29, 5, 6, 34, tzinfo=timezone.utc)

    assert ui_shared._format_timestamp(value) == "Apr 29, 2026, 5:06 AM"


def test_change_state_downgrades_old_unapplied_create_to_unsynced() -> None:
    now = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
    created_at = now - timedelta(hours=25)

    assert (
        ui_shared._change_state(
            created_at=created_at, updated_at=None, last_applied_at=None, now=now
        )
        == "unsynced"
    )


def test_change_state_downgrades_old_post_apply_create_to_unsynced() -> None:
    now = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
    last_applied_at = now - timedelta(days=3)
    created_at = now - timedelta(hours=25)

    assert (
        ui_shared._change_state(
            created_at=created_at,
            updated_at=None,
            last_applied_at=last_applied_at,
            now=now,
        )
        == "unsynced"
    )


def test_normalize_backend_form_sandbox_image_defaults_app_image() -> None:
    sandbox_image = _normalize_backend_form_sandbox_image(kind="app", sandbox_image="")

    assert sandbox_image == DEFAULT_UI_APP_SANDBOX_PROFILE


def test_normalize_backend_form_sandbox_image_clears_static_image() -> None:
    sandbox_image = _normalize_backend_form_sandbox_image(
        kind="static",
        sandbox_image="docker.io/library/ubuntu:24.04",
    )

    assert sandbox_image is None


def test_normalize_backend_form_sandbox_image_unknown_kind_clears_container_fields() -> (
    None
):
    sandbox_image = _normalize_backend_form_sandbox_image(
        kind="other",
        sandbox_image="docker.io/library/ubuntu:24.04",
    )

    assert sandbox_image is None


def test_backend_form_resource_size_choice_maps_to_existing_fields() -> None:
    payload = ui_output._backend_create_payload_from_form(
        name="web",
        kind="app",
        port="12000",
        static_root="",
        sandbox_profile="",
        sandbox_image="docker.io/library/ubuntu:24.04",
        handoff_port=8000,
        healthcheck_mode="tcp",
        healthcheck_path="/",
        healthcheck_host_header="",
        resource_mode="auto",
        resource_size="custom",
        memory_high_override="512M",
        memory_max_override="768M",
        cpu_quota_override="100%",
        volumes_json="[]",
        notes="",
        enabled=True,
        input_ids=[],
    )

    assert payload.resource_mode == "manual"
    assert payload.resource_size == "small"
    assert payload.memory_high_override == "512M"
    assert payload.memory_max_override == "768M"
    assert payload.cpu_quota_override == "100%"

    preset_payload = ui_output._backend_create_payload_from_form(
        name="web-large",
        kind="app",
        port="12001",
        static_root="",
        sandbox_profile="",
        sandbox_image="docker.io/library/ubuntu:24.04",
        handoff_port=8000,
        healthcheck_mode="tcp",
        healthcheck_path="/",
        healthcheck_host_header="",
        resource_mode="auto",
        resource_size="large",
        memory_high_override="512M",
        memory_max_override="768M",
        cpu_quota_override="100%",
        volumes_json="[]",
        notes="",
        enabled=True,
        input_ids=[],
    )

    assert preset_payload.resource_mode == "manual"
    assert preset_payload.resource_size == "large"
    assert preset_payload.memory_high_override is None
    assert preset_payload.memory_max_override is None
    assert preset_payload.cpu_quota_override is None


def test_debug_kv_dump_renders_multiline_values() -> None:
    rendered = ui_shared._debug_kv_dump(
        [
            ("name", "web"),
            ("env", {"PORT": "8337"}),
            ("command", "python3 -m app"),
        ]
    )

    assert "name: web" in rendered
    assert "env:" in rendered
    assert '"PORT": "8337"' in rendered
    assert "command: python3 -m app" in rendered


def test_debug_kv_dump_falls_back_for_non_serializable_values() -> None:
    circular: list[object] = []
    circular.append(circular)

    rendered = ui_shared._debug_kv_dump([("bad", circular)])

    assert "bad:" in rendered


def test_save_job_error_meta_uses_only_reported_error_reference() -> None:
    assert view_models._save_job_error_meta({}) is None
    assert view_models._save_job_error_meta(
        {"error_code": "CNC-02099", "error_inst": "APPLY001"}
    ) == {"label": "error", "value": "CNC-02099-APPLY001"}


def test_save_job_summary_includes_change_rows() -> None:
    summary = ui_shared._save_job_summary(
        {
            "id": 21,
            "status": "success",
            "message": "apply completed",
            "details": {
                "route_contracts": [
                    {
                        "input_kind": "domain",
                        "input_value": "web.example.com",
                        "enabled": True,
                        "backend_names": ["web"],
                        "target": "http://127.0.0.1:12000",
                    }
                ],
                "backend_contracts": [
                    {
                        "backend": "web",
                        "kind": "app",
                        "enabled": True,
                        "port": 12000,
                        "handoff_port": 8337,
                        "sandbox_profile": "ubuntu-24.04-systemd",
                        "healthcheck_path": "/health",
                    }
                ],
                "ssh_backends": ["web"],
                "runtime_assets": {"changed_units": ["cnc-backend-alerts.service"]},
            },
            "created_at": "2026-04-21T10:00:00+00:00",
        }
    )

    assert summary is not None
    assert summary["change_rows"][0]["label"] == "routes"
    assert any(item["label"] == "backend ssh" for item in summary["change_rows"])
    assert any(item["label"] == "host assets" for item in summary["change_rows"])


def test_save_job_summary_success_counts_outputs_and_paths() -> None:
    summary = ui_shared._save_job_summary(
        {
            "id": 42,
            "status": "success",
            "message": "apply completed",
            "created_at": "2026-03-29T12:00:00",
            "details": {
                "nginx_files": ["a.conf", "b.conf"],
                "tailscale_paths": [
                    {"path": "/app1", "target": "http://127.0.0.1:12001"}
                ],
                "app_containers_running": ["cnc-app-web"],
            },
        }
    )

    assert summary is not None
    assert summary["headline"] == "Host updated."
    assert summary["note"] == "2 nginx, 1 tailnet, 1 app runtime"
    assert {"label": "status", "value": "saved"} not in summary["meta"]
    assert {
        "label": "time",
        "value": "Mar 29, 2026, 12:00 PM",
        "raw": "2026-03-29T12:00:00+00:00",
    } in summary["meta"]


def test_save_job_summary_failure_includes_phase_rows_and_dump() -> None:
    summary = ui_shared._save_job_summary(
        {
            "id": 43,
            "status": "error",
            "message": "apply failed",
            "created_at": "2026-03-29T12:01:00",
            "details": {
                "phase": "app_publish",
                "error": "loopback healthcheck failed",
                "app_reconcile_phases": {
                    "web": [
                        {"phase": "prepare", "status": "succeeded", "details": {}},
                        {
                            "phase": "publish",
                            "status": "failed",
                            "details": {"error": "loopback failed"},
                        },
                    ]
                },
            },
        }
    )

    assert summary is not None
    assert summary["headline"] == "App publish failed"
    assert summary["note"] == "loopback healthcheck failed"
    assert summary["phase_rows"][0]["backend"] == "web"
    assert "publish failed" in summary["phase_rows"][0]["summary"]
    assert "failed phase: app publish" in (summary["detail_dump"] or "")
    assert "root cause: loopback healthcheck failed" in (summary["detail_dump"] or "")


def test_save_job_summary_healthcheck_failure_has_instance_and_specific_probe() -> None:
    summary = ui_shared._save_job_summary(
        {
            "id": 97,
            "status": "error",
            "message": "apply failed",
            "created_at": "2026-04-28T02:22:59",
            "details": {
                "phase": "app_healthcheck",
                "backend": "web",
                "error": "http probe returned 400",
                "url": "http://127.0.0.1:12001/health",
                "http_status": 400,
                "host_header": "web.example.com",
                "error_code": "CNC-02099",
                "error_name": "APPLY_FAILED",
                "error_inst": "HEALTH01",
            },
        }
    )

    assert summary is not None
    assert summary["meta"][0] == {
        "label": "error",
        "value": "CNC-02099-HEALTH01",
    }
    assert {
        "label": "time",
        "value": "Apr 28, 2026, 2:22 AM",
        "raw": "2026-04-28T02:22:59+00:00",
    } in summary["meta"]
    assert summary["headline"] == "Web app healthcheck failed"
    assert summary["note"] == (
        "healthcheck returned HTTP 400 at http://127.0.0.1:12001/health "
        "with Host web.example.com"
    )
    assert "root cause: healthcheck returned HTTP 400" in (summary["detail_dump"] or "")


def test_save_job_summary_partial_failure_marks_host_state() -> None:
    summary = ui_shared._save_job_summary(
        {
            "id": 44,
            "status": "error",
            "message": "apply partially failed",
            "created_at": "2026-03-29T12:02:00",
            "details": {
                "phase": "ssh_access",
                "error": "ssh alias update exploded",
                "failure_mode": "partial",
                "manual_review_required": True,
                "completed_phases": ["app_runtime"],
                "live_mutation_phases": ["app_runtime", "ssh_access"],
                "nginx_rollback": {
                    "mode": "remove_unloaded_candidate",
                    "attempted": False,
                    "status": "not_staged",
                },
            },
        }
    )

    assert summary is not None
    assert summary["status_label"] == "failed"
    assert summary["headline"] == "SSH access failed"
    assert summary["note"] == "ssh alias update exploded"
    assert "nginx_rollback" in (summary["detail_dump"] or "")
    assert "host state: partially updated before failure" in (
        summary["detail_dump"] or ""
    )
    assert "manual review: required" not in (summary["detail_dump"] or "")


def test_save_job_summary_command_failure_prefers_failed_phase_backend_and_stderr() -> (
    None
):
    summary = ui_shared._save_job_summary(
        {
            "id": 85,
            "status": "error",
            "message": "apply partially failed",
            "created_at": "2026-04-20T20:52:06",
            "details": {
                "phase": "command",
                "failed_phase": "app_runtime",
                "error": "command failed",
                "stderr": "Error: error opening /run/crun/xyz/status: No such file or directory",
                "command": [
                    "podman",
                    "update",
                    "--memory-reservation",
                    "686M",
                    "cnc-app-web",
                ],
                "failure_mode": "partial",
                "manual_review_required": True,
            },
        }
    )

    assert summary is not None
    assert summary["headline"] == "Web app runtime failed"
    assert (
        summary["note"]
        == "Error: error opening /run/crun/xyz/status: No such file or directory"
    )
    assert "backend: web" in (summary["detail_dump"] or "")
    assert "failed phase: app runtime" in (summary["detail_dump"] or "")
    assert "command: podman update --memory-reservation 686M cnc-app-web" in (
        summary["detail_dump"] or ""
    )
