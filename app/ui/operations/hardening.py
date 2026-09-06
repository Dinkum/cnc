from __future__ import annotations

from app.config import Settings
from app.logger import get_logger
from app.services.app_hardening import latest_hardening_summary
from app.services.hardening_policy import (
    HardeningPolicy,
    hardening_preview,
    read_hardening_policy,
)
from app.services.operations import OperationHandle, host_mutation_operation
from app.ui.operations.common import background_db_session, operation_backend
from app.ui.operations.output_save import _commit_output_save_apply
from app.ui.progress import _complete_output_save_operation

logger = get_logger("ui")


async def run_hardening_apply(
    settings: Settings,
    operation_id: int,
    backend_id: int,
    *,
    mode: str,
    configuration: str,
    revision: str,
) -> None:
    operation = OperationHandle(
        id=operation_id, kind="ui.backend.hardening", settings=settings
    )
    try:
        async with host_mutation_operation(
            settings,
            kind=operation.kind,
            actor="ui",
            backend_id=backend_id,
            phase="Apply security configuration",
            operation=operation,
        ):
            async with background_db_session(settings) as session:
                backend = await operation_backend(session, backend_id)
                if backend.kind != "app":
                    raise ValueError("Hardening is only available for app outputs.")
                backend_name = backend.name
                summary = await latest_hardening_summary(session, backend_id)
                plan = hardening_preview(backend, summary, mode, configuration)
                if plan["revision"] != revision:
                    raise ValueError(
                        "The output or recommendations changed. Review the configuration again."
                    )
                policy = HardeningPolicy.model_validate(plan["configuration"])
                previous = read_hardening_policy(backend).persisted()
                if policy.persisted() != previous:
                    backend.hardening_previous_json = previous
                backend.hardening_config_json = policy.persisted()
                response = await _commit_output_save_apply(
                    session,
                    settings,
                    operation,
                    operation_name="ui.backend.hardening",
                    backend_id=backend_id,
                    backend_name=backend_name,
                )
                await _complete_output_save_operation(
                    operation,
                    response,
                    success_message="Security configuration applied.",
                    failure_prefix="Security configuration could not be applied",
                    backend_id=backend_id,
                    backend_name=backend_name,
                    extra_details={
                        "hardening_changes": plan["changes"],
                        "hardening_mode": mode,
                    },
                )
    except Exception as exc:
        logger.exception(
            "ui.hardening.apply_failed",
            backend_id=backend_id,
            operation_id=operation_id,
            error=str(exc),
        )
        if not operation.completed:
            await operation.complete(
                "failed", phase="Apply security configuration", error=str(exc)
            )
