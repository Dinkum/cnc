"""certs: the origin TLS certificates nginx serves.

Not a tool in its own right, because its commit point sits inside another
tool's. A certificate is prepared before nginx reloads and only counts as live
once that reload succeeds, so ``prepare`` and ``activate`` are two halves that
the nginx tool calls on either side of its reload.

Keeping the logic here rather than inline in nginx means the certificate rules
live in one file. Keeping the ordering in nginx means the coupling is visible
where it actually exists instead of being hidden behind a second timer, which
is how ``cnc-origin-tls.timer`` came to write the host on its own.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from app.config import Settings
from app.services.apply_core import ApplyFailed
from app.services.cloudflare_api import CloudflareError
from app.services.cloudflare_certificates import prepare_origin_certificates
from app.services.origin_tls import (
    certificate_activated,
    certificate_path,
    prepare_certificate,
)


class Prepared:
    """Certificates written to disk but not yet counted as live."""

    def __init__(self, origin: dict[str, Any], main: str | None) -> None:
        self.origin = origin
        self.main = main

    def __bool__(self) -> bool:
        return bool(self.origin) or self.main is not None


async def prepare(nginx_files: dict[str, str], settings: Settings) -> Prepared:
    """Write any certificate nginx's new config will reference.

    Runs before nginx stages its files, because a config that references a
    certificate that is not there yet will not pass ``nginx -t``.
    """
    try:
        origin = await asyncio.to_thread(
            prepare_origin_certificates, nginx_files, settings
        )
        main = await asyncio.to_thread(
            prepare_certificate,
            nginx_files,
            certificate_path(settings.app_control_dir),
        )
    except (OSError, RuntimeError, CloudflareError, subprocess.SubprocessError) as exc:
        raise ApplyFailed(
            "failed to prepare origin HTTPS certificate",
            phase="sync_files",
            details={"error": str(exc)},
        ) from exc
    return Prepared(origin, main)


def activate(prepared: Prepared, settings: Settings) -> None:
    """Record the prepared certificates as live. Only after nginx reloaded."""
    for path, fingerprint in prepared.origin.items():
        certificate_activated(Path(path), fingerprint)
    if prepared.main:
        certificate_activated(certificate_path(settings.app_control_dir), prepared.main)
