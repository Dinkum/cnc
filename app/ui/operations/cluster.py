from __future__ import annotations

import asyncio

from app.config import Settings
from app.logger import get_logger
from app.services.cluster_transfer import setup_backend_replica, transfer_backend
from app.services.error_reporting import ErrorCode, error_context
from app.services.operations import OperationHandle
from app.ui.errors import operator_action_error
from app.ui.operations.common import background_db_session
from app.ui.progress import _operation_progress_details


logger = get_logger("ui")


async def _run_transfer_operation(
    settings: Settings,
    operation_id: int | None,
    backend_id: int,
    target_node: str,
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
            loop = asyncio.get_running_loop()
            last_progress = {"value": 0}
            progress_updates = []

            def report_transfer_progress(_progress: int, message: str) -> None:
                details = _operation_progress_details(
                    "transferOutput",
                    int(last_progress["value"]),
                    message,
                    phase="Transfer output",
                    substate=message,
                    backend_id=backend_id,
                    target_node=target_node,
                )
                last_progress["value"] = int(details["progress"])
                progress_updates.append(
                    asyncio.run_coroutine_threadsafe(
                        operation.update(
                            status="running",
                            phase="Transfer output",
                            details=details,
                        ),
                        loop,
                    )
                )

            result = await transfer_backend(
                session,
                settings,
                backend_id=backend_id,
                target_node_uid=target_node,
                progress_callback=report_transfer_progress,
            )
            for progress_update in progress_updates:
                try:
                    await asyncio.wrap_future(progress_update)
                except Exception as exc:
                    logger.warning("ui.transfer.progress_update_failed", error=str(exc))
            await operation.complete(
                "success",
                phase="Transfer output",
                details={
                    "progress": 100,
                    "message": f"{result.backend_name} transferred to {result.target_label}.",
                    "flash_success": f"{result.backend_name} transferred to {result.target_label}.",
                    "backend_id": result.backend_id,
                    "backend_name": result.backend_name,
                    "source_node_uid": result.source_node_uid,
                    "target_node_uid": result.target_node_uid,
                    "target_label": result.target_label,
                    "route_target": result.route_target,
                    "messages": result.messages,
                },
            )
    except Exception as exc:
        error_fields = error_context(ErrorCode.CLUSTER_TRANSFER_FAILED)
        flash_error = operator_action_error(
            "Transfer",
            "Unexpected error while transferring the output. Check server logs with this instance.",
            error_fields,
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
            "failed",
            phase="Transfer output",
            error=flash_error,
            details={
                "progress": 100,
                "message": "Transfer failed.",
                "flash_error": flash_error,
                "backend_id": backend_id,
                "target_node": target_node,
                **error_fields,
            },
        )


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
            loop = asyncio.get_running_loop()
            progress_updates = []

            def report_setup_progress(progress: int, message: str) -> None:
                progress_updates.append(
                    asyncio.run_coroutine_threadsafe(
                        operation.update(
                            status="running",
                            phase="Replica setup",
                            details=_operation_progress_details(
                                "replicaSetup",
                                progress,
                                message,
                                phase="Replica setup",
                                substate=message,
                                backend_id=backend_id,
                                target_node=target_node,
                                setup_mode=setup_mode,
                            ),
                        ),
                        loop,
                    ),
                )

            result = await setup_backend_replica(
                session,
                settings,
                backend_id=backend_id,
                target_node_uid=target_node,
                setup_mode=setup_mode,
                progress_callback=report_setup_progress,
            )
            for progress_update in progress_updates:
                try:
                    await asyncio.wrap_future(progress_update)
                except Exception as exc:
                    logger.warning(
                        "ui.replica_setup.progress_update_failed", error=str(exc)
                    )
            await operation.complete(
                "success",
                phase="Replica setup",
                details={
                    "progress": 100,
                    "message": f"{result.backend_name} set up on {result.target_label}.",
                    "flash_success": f"{result.backend_name} set up on {result.target_label}.",
                    "backend_id": result.backend_id,
                    "backend_name": result.backend_name,
                    "target_node_uid": result.target_node_uid,
                    "target_label": result.target_label,
                    "setup_mode": result.setup_mode,
                    "route_target": result.route_target,
                    "messages": result.messages,
                },
            )
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
        await operation.complete(
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
