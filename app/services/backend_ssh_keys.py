"""Named, output-scoped SSH access. Private keys live only in the create response."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import re
import secrets
import string
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.entities import Backend, BackendSshKey
from app.services.commands import run_command_checked
from app.services.operations import host_mutation_operation
from app.services.ssh_access import reconcile_backend_ssh_access


def generate_backend_ssh_keypair(backend_name: str) -> tuple[str, str]:
    with tempfile.TemporaryDirectory(prefix="cnc-backend-ssh-key-") as temp_dir:
        key_path = Path(temp_dir) / "id_ed25519"
        run_command_checked(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                f"cnc-{backend_name}",
                "-f",
                str(key_path),
            ],
            timeout_sec=30,
        )
        return key_path.with_suffix(".pub").read_text().strip(), key_path.read_text()


def parse_public_key(value: str) -> tuple[str, str]:
    """Accept one bare Ed25519 key, never authorized_keys options or extra lines."""
    value = value.strip()
    parts = value.split()
    if (
        len(value) > 4096
        or "\n" in value
        or "\r" in value
        or len(parts) < 2
        or parts[0] != "ssh-ed25519"
    ):
        raise ValueError("Enter one Ed25519 public key beginning with ssh-ed25519.")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError as exc:
        raise ValueError("The public key is not valid base64.") from exc
    # OpenSSH Ed25519 wire format: algorithm string, then exactly 32 key bytes.
    prefix = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
    if not blob.startswith(prefix) or len(blob) != len(prefix) + 32:
        raise ValueError("The public key is not a valid Ed25519 key.")
    fingerprint = "SHA256:" + base64.b64encode(
        hashlib.sha256(blob).digest()
    ).decode().rstrip("=")
    return " ".join(parts[:2]), fingerprint


async def list_backend_ssh_keys(
    session: AsyncSession, backend_id: int
) -> list[BackendSshKey]:
    return list(
        (
            await session.scalars(
                select(BackendSshKey)
                .where(
                    BackendSshKey.backend_id == backend_id,
                    BackendSshKey.revoked_at.is_(None),
                )
                .order_by(BackendSshKey.created_at, BackendSshKey.id)
            )
        ).all()
    )


def ssh_key_metadata(key: BackendSshKey) -> dict[str, str]:
    created = key.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return {
        "id": key.id,
        "name": key.name,
        "fingerprint": key.fingerprint,
        "filename": key.filename,
        "created_at": created.isoformat(),
    }


async def _allocate_key_id(session: AsyncSession) -> str:
    for _ in range(100):
        candidate = "".join(
            secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6)
        )
        if await session.get(BackendSshKey, candidate) is None:
            return candidate
    raise RuntimeError("Could not allocate a unique SSH key ID.")


async def _mutate_key(
    session: AsyncSession,
    settings: Settings,
    backend_id: int,
    *,
    name: str = "",
    public_key: str | None = None,
    revoke_id: str | None = None,
) -> dict[str, object]:
    # The operation writer opens its own connection; release any page read first.
    await session.rollback()
    async with host_mutation_operation(
        settings,
        kind="backend_ssh_key",
        actor="ui",
        backend_id=backend_id,
        phase="Update SSH access",
    ):
        backend = await session.get(Backend, backend_id)
        if backend is None or backend.kind != "app":
            raise LookupError("App output not found.")
        enabled = list(
            (
                await session.scalars(
                    select(Backend)
                    .where(Backend.kind == "app", Backend.enabled.is_(True))
                    .order_by(Backend.id)
                )
            ).all()
        )
        active = await list_backend_ssh_keys(session, backend_id)
        private_key = None
        if revoke_id is not None:
            key = await session.get(BackendSshKey, revoke_id)
            if key is None or key.backend_id != backend_id:
                raise LookupError("SSH key not found.")
            key.revoked_at = key.revoked_at or datetime.now(timezone.utc)
        else:
            name = name.strip()
            if (
                not name
                or len(name) > 80
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
            ):
                raise ValueError(
                    "Enter a key name of 1–80 characters without control characters."
                )
            if any(item.name.casefold() == name.casefold() for item in active):
                raise ValueError("A key with this name already exists.")
            if len(active) >= 100:
                raise ValueError(
                    "Revoke an unused key before adding more keys to this output."
                )
            if public_key is None:
                public_key, private_key = await asyncio.to_thread(
                    generate_backend_ssh_keypair, backend.name
                )
            public_key, fingerprint = parse_public_key(public_key)
            if any(item.fingerprint == fingerprint for item in active):
                raise ValueError("This public key already has access to this output.")
            key_id = await _allocate_key_id(session)
            safe_name = re.sub(r"[^a-zA-Z0-9_-]", "-", backend.name)
            key = BackendSshKey(
                id=key_id,
                backend_id=backend_id,
                name=name,
                public_key=public_key,
                fingerprint=fingerprint,
                filename=f"cnc-{safe_name}-ssh-{key_id}",
                created_at=datetime.now(timezone.utc),
            )
            session.add(key)
            active.append(key)
        backend.ssh_public_key = (
            "\n".join(item.public_key for item in active if item.revoked_at is None)
            or None
        )
        backend.ssh_private_key = None
        applied = False
        try:
            await session.flush()
            if backend.enabled:
                await reconcile_backend_ssh_access(enabled, settings)
                applied = True
            metadata = ssh_key_metadata(key)
            await session.commit()
        except BaseException:
            await session.rollback()
            # Re-read committed state: a commit exception can occur after SQLite
            # accepted the commit, so blindly restoring the old keys could regrant access.
            if applied:
                persisted = list(
                    (
                        await session.scalars(
                            select(Backend)
                            .where(Backend.kind == "app", Backend.enabled.is_(True))
                            .order_by(Backend.id)
                        )
                    ).all()
                )
                await reconcile_backend_ssh_access(persisted, settings)
            raise
        result: dict[str, object] = {"key": metadata}
        if private_key is not None:
            result["private_key"] = private_key
        return result


async def mutate_backend_ssh_key(
    session: AsyncSession,
    settings: Settings,
    backend_id: int,
    *,
    name: str = "",
    public_key: str | None = None,
    revoke_id: str | None = None,
) -> dict[str, object]:
    # Finish reconciliation/commit (or rollback) before cancellation releases the lock.
    task = asyncio.create_task(
        _mutate_key(
            session,
            settings,
            backend_id,
            name=name,
            public_key=public_key,
            revoke_id=revoke_id,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
