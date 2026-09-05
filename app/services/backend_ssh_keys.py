from __future__ import annotations

import tempfile
from pathlib import Path

from app.models.entities import Backend
from app.services.commands import run_command_checked


def generate_backend_ssh_keypair(backend_name: str) -> tuple[str, str]:
    comment = f"cnc-{backend_name}"
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
                comment,
                "-f",
                str(key_path),
            ],
            timeout_sec=30,
        )
        private_key = key_path.read_text(encoding="utf-8")
        public_key = key_path.with_suffix(".pub").read_text(encoding="utf-8").strip()
    return public_key, private_key


def ensure_backend_ssh_keypair(backend: Backend) -> bool:
    if backend.ssh_public_key and backend.ssh_private_key:
        return False
    public_key, private_key = generate_backend_ssh_keypair(backend.name)
    backend.ssh_public_key = public_key
    backend.ssh_private_key = private_key
    return True
