"""add per-backend SSH keys

Revision ID: 0035_backend_ssh_keys
Revises: 0034_backend_replica_readiness
Create Date: 2026-07-06
"""

from alembic import op
import sqlalchemy as sa


revision = "0035_backend_ssh_keys"
down_revision = "0034_backend_replica_readiness"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    columns = _columns("backends")
    if "ssh_public_key" not in columns:
        op.add_column("backends", sa.Column("ssh_public_key", sa.Text(), nullable=True))
    if "ssh_private_key" not in columns:
        op.add_column(
            "backends", sa.Column("ssh_private_key", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    columns = _columns("backends")
    if "ssh_private_key" in columns:
        op.drop_column("backends", "ssh_private_key")
    if "ssh_public_key" in columns:
        op.drop_column("backends", "ssh_public_key")
