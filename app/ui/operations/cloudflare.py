from __future__ import annotations

from app.config import Settings
from app.logger import get_logger
from app.services.mutation_queue import mutation_runner
from app.services.cloudflare_api import CloudflareError
from app.services.cloudflare_domains import (
    apply_domain_preview,
    consume_preview,
    save_domain_status,
)
from app.services.input_commands import require_input
from app.services.operations import OperationHandle, host_mutation_operation
from app.ui.operations.common import background_db_session

logger = get_logger("cloudflare.domains")


@mutation_runner("cloudflare_domain")
async def run_domain_operation(
    settings: Settings, operation_id: int, input_id: int, preview_id: str
) -> None:
    operation = OperationHandle(
        id=operation_id, kind="cloudflare_domain", settings=settings
    )
    try:
        async with host_mutation_operation(
            settings,
            kind="cloudflare_domain",
            actor="ui",
            operation=operation,
            phase="Checking preview",
        ):
            async with background_db_session(settings) as session:
                item = await require_input(session, input_id)
                hostname = item.hostname
                preview = consume_preview(settings, item, preview_id)
                try:
                    await apply_domain_preview(
                        session, settings, item, preview, operation
                    )
                except Exception:
                    await session.rollback()
                    item = await require_input(session, input_id)
                    save_domain_status(
                        settings,
                        item,
                        status="Needs attention",
                        error="Cloudflare changes did not finish. Existing DNS and certificate state may be partially applied; preview again to check.",
                    )
                    raise
            await operation.complete(
                "success",
                phase="Ready",
                details={"message": f"Cloudflare settings saved for {hostname}."},
            )
    except (CloudflareError, LookupError, ValueError) as exc:
        if not operation.completed:
            await operation.complete(
                "failed",
                phase="Needs attention",
                error=str(exc),
                details={"message": str(exc)},
            )
        logger.warning("cloudflare.domain.failed", input_id=input_id, error=str(exc))
    except Exception:
        if not operation.completed:
            await operation.complete(
                "failed",
                phase="Needs attention",
                error="Cloudflare changes did not finish. Preview again to check the current state.",
            )
        logger.exception("cloudflare.domain.failed", input_id=input_id)
