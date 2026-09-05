"""add optional backend update command

Revision ID: 0008_backend_update_command
Revises: 0007_auto_size_resource_policy
Create Date: 2026-04-02 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008_backend_update_command"
down_revision = "0007_auto_size_resource_policy"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    return {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    backend_columns = _column_names("backends")
    with op.batch_alter_table("backends") as batch:
        if "update_command" not in backend_columns:
            batch.add_column(sa.Column("update_command", sa.Text(), nullable=True))


def downgrade() -> None:
    backend_columns = _column_names("backends")
    with op.batch_alter_table("backends") as batch:
        if "update_command" in backend_columns:
            batch.drop_column("update_command")
