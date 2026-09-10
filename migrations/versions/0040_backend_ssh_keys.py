"""Name output SSH keys and discard stored private key material."""

import base64
import hashlib
import secrets
import string

from alembic import op
import sqlalchemy as sa

revision = "0040_backend_ssh_keys"
down_revision = "0039_command_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table(
        "backend_ssh_keys",
        sa.Column("id", sa.String(6), primary_key=True),
        sa.Column(
            "backend_id",
            sa.Integer(),
            sa.ForeignKey("backends.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(80), nullable=False),
        sa.Column("filename", sa.String(320), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_backend_ssh_keys_backend_id", "backend_ssh_keys", ["backend_id"]
    )
    connection = op.get_bind()
    issued: set[str] = set()
    for backend in connection.execute(
        sa.text(
            "SELECT id, name, ssh_public_key, created_at FROM backends WHERE ssh_public_key IS NOT NULL"
        )
    ).mappings():
        for number, public_key in enumerate(
            backend["ssh_public_key"].strip().splitlines(), 1
        ):
            if not public_key.strip():
                continue
            while True:
                key_id = "".join(
                    secrets.choice(string.ascii_lowercase + string.digits)
                    for _ in range(6)
                )
                if key_id not in issued:
                    issued.add(key_id)
                    break
            try:
                blob = base64.b64decode(public_key.split()[1], validate=True)
                fingerprint = "SHA256:" + base64.b64encode(
                    hashlib.sha256(blob).digest()
                ).decode().rstrip("=")
            except (ValueError, IndexError):
                # Preserve access even if a legacy value cannot be fingerprinted.
                fingerprint = "Unavailable"
            connection.execute(
                table.insert().values(
                    id=key_id,
                    backend_id=backend["id"],
                    name="Existing shared key"
                    if number == 1
                    else f"Existing shared key {number}",
                    public_key=public_key.strip(),
                    fingerprint=fingerprint,
                    filename=f"cnc-{backend['name']}-ssh-{key_id}",
                )
            )
    connection.execute(sa.text("UPDATE backends SET ssh_private_key = NULL"))


def downgrade() -> None:
    # Private keys cannot be reconstructed. Public authorized keys remain on backends.
    op.drop_index("ix_backend_ssh_keys_backend_id", table_name="backend_ssh_keys")
    op.drop_table("backend_ssh_keys")
