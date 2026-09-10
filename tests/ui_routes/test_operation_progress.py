import pytest

import app.ui.view_models as ui_view_models

from .support import (
    ApplyResponse,
    create_backend_progress_value,
    create_output_progress_plan,
    delete_input_progress_plan,
    delete_output_progress_plan,
    input_progress_pipelines,
    operation_progress_pipelines,
    operation_progress_value,
    output_save_progress_steps,
    progress_plan_value,
)


def test_save_apply_feedback_success_includes_save_job_id() -> None:
    success, error = ui_view_models._save_apply_feedback(
        ApplyResponse(
            status="success",
            message="apply completed",
            details={},
            run_id=42,
        ),
        success_message="Input saved.",
        failure_prefix="Input saved",
    )

    assert success == "Input saved. Host updated."
    assert error is None


def test_save_apply_feedback_failure_surfaces_phase() -> None:
    success, error = ui_view_models._save_apply_feedback(
        ApplyResponse(
            status="error",
            message="apply failed",
            details={
                "phase": "nginx_validate",
                "error": "bad config",
                "error_code": "CNC-02001",
                "error_name": "APPLY_NGINX_CONFIG_INVALID",
                "error_inst": "NGINX001",
            },
            run_id=18,
        ),
        success_message="Output saved.",
        failure_prefix="Output saved",
    )

    assert success is None
    assert (
        error
        == "Save failed (Error CNC-02001-NGINX001). Existing config is still active. Reason: nginx validate failed: bad config"
    )


def test_save_apply_feedback_uses_error_instance_when_apply_run_is_missing() -> None:
    success, error = ui_view_models._save_apply_feedback(
        ApplyResponse(
            status="error",
            message="apply failed",
            details={
                "phase": "apply",
                "error": "apply worker exploded",
                "error_code": "CNC-02099",
                "error_inst": "7K2Q9M4D",
            },
            run_id=None,
        ),
        success_message="Output created.",
        failure_prefix="Output saved",
    )

    assert success is None
    assert error == (
        "Save failed (Error CNC-02099-7K2Q9M4D). Existing config is still active. "
        "Reason: apply failed: apply worker exploded"
    )


def test_save_apply_feedback_partial_failure_reports_possible_host_changes() -> None:
    success, error = ui_view_models._save_apply_feedback(
        ApplyResponse(
            status="error",
            message="apply partially failed",
            details={
                "phase": "ssh_access",
                "error": "ssh alias update exploded",
                "failure_mode": "partial",
                "manual_review_required": True,
                "error_code": "CNC-02099",
                "error_name": "APPLY_FAILED",
                "error_inst": "APPLY002",
            },
            run_id=19,
        ),
        success_message="Output saved.",
        failure_prefix="Output saved",
    )

    assert success is None
    assert error == (
        "Save failed (Error CNC-02099-APPLY002). "
        "Some host changes may already be active. Reason: ssh access failed: "
        "ssh alias update exploded"
    )


def test_save_apply_feedback_self_audit_surfaces_blocking_finding() -> None:
    success, error = ui_view_models._save_apply_feedback(
        ApplyResponse(
            status="error",
            message="apply partially failed",
            details={
                "phase": "control_plane_self_audit",
                "error": "control-plane self-audit failed",
                "failure_mode": "partial",
                "manual_review_required": True,
                "findings": [
                    {
                        "check": "tailscale_admin_exposure",
                        "severity": "blocking",
                        "message": "tailscale serve exposes 127.0.0.1:9090 via an unapproved mapping",
                    }
                ],
                "error_code": "CNC-02099",
                "error_name": "APPLY_FAILED",
                "error_inst": "APPLY003",
            },
            run_id=20,
        ),
        success_message="Output saved.",
        failure_prefix="Output saved",
    )

    assert success is None
    assert error == (
        "Save failed (Error CNC-02099-APPLY003). "
        "Some host changes may already be active. Reason: control plane self audit failed: "
        "tailscale serve exposes 127.0.0.1:9090 via an unapproved mapping"
    )


def test_input_progress_pipelines_are_ordered_state_substate_catalogs() -> None:
    pipelines = input_progress_pipelines()

    assert set(pipelines) == {"createInput", "saveInput", "deleteInput"}
    for steps in pipelines.values():
        progress_values = [int(step["progress"]) for step in steps]
        assert progress_values == sorted(progress_values)
        assert 0 < progress_values[0] < progress_values[-1] < 100
        assert all(
            step["headline"] and step["substep"] and step["note"] for step in steps
        )
        assert all(int(step["state_weight"]) <= 3 for step in steps)
        assert all(int(step["substep_weight"]) <= 3 for step in steps)
        for step in steps:
            state_weight = int(step["state_weight"])
            substep_weight = int(step["substep_weight"])
            if step["headline"] == "Apply host access":
                assert state_weight == 2
                assert substep_weight == (
                    2 if step["substep"] == "Validating host proxy config" else 1
                )
            else:
                assert state_weight == 1
                assert substep_weight == 1

    create_pairs = {
        (str(step["headline"]), str(step["substep"]))
        for step in pipelines["createInput"]
    }
    assert ("Validate input", "Reading submitted config") in create_pairs
    assert ("Save desired state", "Staging input config") in create_pairs
    assert ("Save desired state", "Waiting for host mutation lock") in create_pairs
    assert ("Save desired state", "Building host plan") in create_pairs
    assert ("Apply host access", "Reconciling host routes") in create_pairs
    assert ("Apply host access", "Updating tailnet services") in create_pairs
    assert ("Finalize", "Refreshing dashboard") in create_pairs


def test_operation_specific_progress_plans_reweight_reachable_steps() -> None:
    static_plan = create_output_progress_plan(
        backend_kind="static",
        input_kinds=("domain",),
        include_runtime=False,
        include_runtime_inspection=False,
        include_host_assets=False,
        include_shield=False,
        include_cluster=False,
        include_tailnet_paths=False,
        include_tailnet_services=False,
    )
    static_pairs = {
        (str(step["headline"]), str(step["substep"])) for step in static_plan["steps"]
    }
    assert ("Provision guest", "Preparing package catalog") not in static_pairs
    assert ("Provision guest", "Installing guest runtime") not in static_pairs
    assert ("Provision guest", "Installing guest tools") not in static_pairs
    assert ("Provision guest", "Cleaning package cache") not in static_pairs
    assert ("Apply host access", "Updating tailnet paths") not in static_pairs
    assert ("Verify output", "Auditing control plane") in static_pairs
    assert progress_plan_value(
        static_plan,
        phase="Save desired state",
        message="Waiting for host mutation lock",
    ) > create_backend_progress_value(
        "Save desired state", substep="Waiting for host mutation lock"
    )

    domain_delete_plan = delete_input_progress_plan(input_kind="domain")
    tailnet_delete_plan = delete_input_progress_plan(input_kind="tailnet_path")
    domain_pairs = {
        (str(step["headline"]), str(step["substep"]))
        for step in domain_delete_plan["steps"]
    }
    tailnet_pairs = {
        (str(step["headline"]), str(step["substep"]))
        for step in tailnet_delete_plan["steps"]
    }
    assert ("Apply host access", "Updating tailnet paths") not in domain_pairs
    assert ("Apply host access", "Updating tailnet paths") in tailnet_pairs
    assert ("Save desired state", "Waiting for host mutation lock") in domain_pairs
    assert ("Save desired state", "Building host plan") in domain_pairs
    assert progress_plan_value(
        domain_delete_plan,
        phase="Apply host access",
        message="Reconciling host routes",
    ) > progress_plan_value(
        tailnet_delete_plan,
        phase="Apply host access",
        message="Reconciling host routes",
    )

    app_output_delete_plan = delete_output_progress_plan(backend_kind="app")
    static_output_delete_plan = delete_output_progress_plan(backend_kind="static")
    app_output_delete_pairs = {
        (str(step["headline"]), str(step["substep"]))
        for step in app_output_delete_plan["steps"]
    }
    static_output_delete_pairs = {
        (str(step["headline"]), str(step["substep"]))
        for step in static_output_delete_plan["steps"]
    }
    assert ("Plan runtime", "Removing app runtime") in app_output_delete_pairs
    assert ("Plan runtime", "Removing app runtime") not in static_output_delete_pairs
    assert (
        "Apply host access",
        "Cleaning backend SSH access",
    ) in app_output_delete_pairs
    assert (
        "Apply host access",
        "Cleaning backend SSH access",
    ) not in static_output_delete_pairs


def test_output_save_progress_steps_are_weighted_defaults() -> None:
    steps = output_save_progress_steps()
    progress_values = [int(step["progress"]) for step in steps]
    pairs = {(str(step["headline"]), str(step["substep"])) for step in steps}

    assert progress_values == sorted(progress_values)
    assert 0 < progress_values[0] < progress_values[-1] < 100
    assert all(step["headline"] and step["substep"] and step["note"] for step in steps)
    assert ("Validate output", "Reading output form") in pairs
    assert ("Prepare host", "Checking tailnet service host") in pairs
    assert ("Plan runtime", "Inspecting app container") in pairs
    assert ("Prepare Shield", "Preparing Shield gate") in pairs
    assert ("Apply host access", "Reloading host proxy") in pairs
    assert ("Apply cluster", "Syncing follower ingress") in pairs
    assert ("Verify output", "Auditing control plane") in pairs


def test_operation_progress_pipelines_are_ordered_state_substate_catalogs() -> None:
    pipelines = operation_progress_pipelines()

    assert set(pipelines) == {
        "outputSave",
        "backup",
        "restore",
        "deleteBackup",
        "importBackup",
        "clone",
        "deleteOutput",
        "transferOutput",
        "replicaSetup",
        "updateCnc",
    }
    for name, steps in pipelines.items():
        progress_values = [int(step["progress"]) for step in steps]
        assert progress_values == sorted(progress_values)
        assert 0 < progress_values[0] < progress_values[-1] < 100
        assert all(
            step["headline"] and step["substep"] and step["note"] for step in steps
        )
        assert all(int(step["state_weight"]) <= 3 for step in steps)
        assert all(int(step["substep_weight"]) <= 3 for step in steps)
        if name == "deleteOutput":
            assert any(int(step["state_weight"]) > 1 for step in steps)
            assert any(int(step["substep_weight"]) > 1 for step in steps)
        else:
            assert all(int(step["state_weight"]) == 1 for step in steps)
            assert all(int(step["substep_weight"]) == 1 for step in steps)

    assert (
        operation_progress_value(
            "clone", phase="Clone output", message="Restoring guest and data", current=0
        )
        == 60
    )
    assert (
        operation_progress_value(
            "deleteOutput",
            phase="Apply host access",
            message="Cleaning backend SSH access",
        )
        == 69
    )
    assert (
        operation_progress_value(
            "outputSave", phase="Apply host access", message="Reloading host proxy"
        )
        > 50
    )
    assert (
        operation_progress_value(
            "transferOutput",
            phase="Verify target",
            message="Checking target health",
        )
        > 50
    )
    assert (
        operation_progress_value(
            "updateCnc",
            phase="Verify update",
            message="Refreshing version card",
        )
        == 96
    )
    assert operation_progress_value("backup", message="unmatched", current=77) == 77


@pytest.mark.asyncio
async def test_input_delete_failure_preserves_cause_phase_and_reference():
    from app.ui.progress import _complete_input_operation

    class Recorder:
        async def complete(self, status, **kwargs):
            self.status = status
            self.result = kwargs

    operation = Recorder()
    await _complete_input_operation(
        operation,
        ApplyResponse(
            status="error",
            message="apply failed",
            run_id=42,
            details={
                "phase": "storage_preflight",
                "failed_backend": "web",
                "operator_message": "Writable storage lacks a private parent directory.",
                "error_code": "CNC-02099",
                "error_inst": "ABCD1234",
                "failure_mode": "clean",
            },
        ),
        success_message="Input deleted.",
        failure_prefix="Input deleted",
        input_value="example",
    )
    assert operation.status == "failed"
    assert operation.result["phase"] == "storage_preflight"
    message = operation.result["error"]
    assert "Couldn't delete input example" in message
    assert "web" in message
    assert "private parent" in message
    assert "CNC-02099-ABCD1234" in message
    assert "input was retained" in message
    assert "Refreshing dashboard" not in str(operation.result)
