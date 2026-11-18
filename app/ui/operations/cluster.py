from __future__ import annotations

import asyncio

from app.config import Settings
from app.logger import get_logger
from app.services.mutation_queue import mutation_runner
from app.services.cluster_transfer import (
    TransferCommitUnknown,
    setup_backend_replica,
    transfer_backend,
)
from app.services.error_reporting import ErrorCode, error_context
from app.services.operation_runtime import RUNNING_PROGRESS_MAX
from app.services.operations import OperationHandle
from app.services.thread_workers import run_to_completion
from app.ui.errors import operator_action_error, operator_error_message
from app.ui.operations.common import background_db_session
from app.ui.progress import _operation_progress_details


logger = get_logger("ui")


@mutation_runner("transfer_backend")
async def _run_transfer_operation(
    settings: Settings,
    operation_id: int | None,
    backend_id: int,
    target_node: str,
    *,
    pause_source: bool,
) -> None:
    operation = OperationHandle(
        id=operation_id, kind="transfer_backend", settings=settings
    )
    try:
        await operation.update(
            status="running",
            phase="Confirm transfer",
            details=_operation_progress_details(
                "transferOutput",
                0,
                "Reading transfer request.",
                phase="Confirm transfer",
                substate="Reading transfer request",
                backend_id=backend_id,
                target_node=target_node,
            ),
        )
        async with background_db_session(settings) as session:
            result = await transfer_backend(
                session,
                settings,
                backend_id=backend_id,
                target_node_uid=target_node,
                pause_source=pause_source,
                operation_handle=operation,
            )
            transferred = f"{result.backend_name} transferred to {result.target_label}."
            if not pause_source:
                transferred += (
                    " It kept running during the copy, so writes made after the"
                    " snapshot were not carried over."
                )
            await operation.complete(
                "success",
                phase="Transfer output",
                details={
                    "progress": 100,
                    "message": transferred,
                    "flash_success": transferred,
                    "backend_id": result.backend_id,
                    "backend_name": result.backend_name,
                    "source_node_uid": result.source_node_uid,
                    "target_node_uid": result.target_node_uid,
                    "target_label": result.target_label,
                    "route_target": result.route_target,
                    "messages": result.messages,
                    "source_paused": pause_source,
                },
            )
    except asyncio.CancelledError:
        await operation.complete(
            "cancelled", phase="Transfer output", error="Transfer cancelled."
        )
        raise
    except Exception as exc:
        uncertain = isinstance(exc, TransferCommitUnknown)
        error_fields = error_context(ErrorCode.CLUSTER_TRANSFER_FAILED)
        flash_error = (
            operator_error_message(str(exc), error_fields)
            if uncertain
            else operator_action_error(
                "Transfer",
                "Unexpected error while transferring the output. Check server logs with this instance.",
                error_fields,
            )
        )
        logger.exception(
            "ui.transfer.worker_failed",
            backend_id=backend_id,
            operation_id=operation_id,
            target_node=target_node,
            **error_fields,
            error=str(exc),
        )
        await operation.complete(
            "partial" if uncertain else "failed",
            phase="Verify placement" if uncertain else "Transfer output",
            error=flash_error,
            details={
                "progress": 100,
                "message": "Verify placement before retrying."
                if uncertain
                else "Transfer failed.",
                **(exc.details if uncertain else {}),
                "flash_error": flash_error,
                "backend_id": backend_id,
                "target_node": target_node,
                **error_fields,
            },
        )


@mutation_runner("setup_backend_replica")
async def _run_replica_setup_operation(
    settings: Settings,
    operation_id: int | None,
    backend_id: int,
    target_node: str,
    setup_mode: str,
) -> None:
    operation = OperationHandle(
        id=operation_id, kind="setup_backend_replica", settings=settings
    )

    def report_setup_progress(progress: int, message: str) -> None:
        operation.publish_progress(
            status="running",
            phase="Replica setup",
            details=_operation_progress_details(
                "replicaSetup",
                min(progress, RUNNING_PROGRESS_MAX),
                message,
                phase="Replica setup",
                backend_id=backend_id,
                target_node=target_node,
                setup_mode=setup_mode,
            ),
        )

    try:
        await operation.update(
            status="running",
            phase="Replica setup",
            details=_operation_progress_details(
                "replicaSetup",
                4,
                "Reading setup request.",
                phase="Setup request",
                substate="Reading setup request",
                backend_id=backend_id,
                target_node=target_node,
                setup_mode=setup_mode,
            ),
        )
        async with background_db_session(settings) as session:
            result = await setup_backend_replica(
                session,
                settings,
                backend_id=backend_id,
                target_node_uid=target_node,
                setup_mode=setup_mode,
                progress_callback=report_setup_progress,
            )
    except asyncio.CancelledError:
        message = "Replica setup was interrupted. Check node readiness before retrying."
        await run_to_completion(
            operation.complete(
                "cancelled",
                phase="Replica setup",
                error=message,
                details={"progress": 100, "message": message},
            )
        )
        raise
    except Exception as exc:
        error_fields = error_context(ErrorCode.CLUSTER_REPLICA_SETUP_FAILED)
        flash_error = operator_action_error(
            "Replica setup",
            "Unexpected error while setting up the selected node. Check server logs with this instance.",
            error_fields,
        )
        logger.exception(
            "ui.replica_setup.worker_failed",
            backend_id=backend_id,
            operation_id=operation_id,
            target_node=target_node,
            setup_mode=setup_mode,
            **error_fields,
            error=str(exc),
        )
        completion = operation.complete(
            "failed",
            phase="Replica setup",
            error=flash_error,
            details={
                "progress": 100,
                "message": "Replica setup failed.",
                "flash_error": flash_error,
                "backend_id": backend_id,
                "target_node": target_node,
                "setup_mode": setup_mode,
                **error_fields,
            },
        )
    else:
        message = f"{result.backend_name} set up on {result.target_label}."
        completion = operation.complete(
            "success",
            phase="Replica setup",
            details={
                "progress": 100,
                "message": message,
                "flash_success": message,
                "backend_id": result.backend_id,
                "backend_name": result.backend_name,
                "target_node_uid": result.target_node_uid,
                "target_label": result.target_label,
                "setup_mode": result.setup_mode,
                "route_target": result.route_target,
                "messages": result.messages,
            },
        )
    await run_to_completion(completion)
