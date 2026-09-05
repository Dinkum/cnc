"""hard cut app backends to sandbox plus handoff

Revision ID: 0009_sandbox_handoff_cutover
Revises: 0008_backend_update_command
Create Date: 2026-04-20 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_sandbox_handoff_cutover"
down_revision = "0008_backend_update_command"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    return {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    backend_columns = _column_names("backends")
    with op.batch_alter_table("backends") as batch:
        if "base_image" in backend_columns and "sandbox_image" not in backend_columns:
            batch.alter_column("base_image", new_column_name="sandbox_image")
        elif "sandbox_image" not in backend_columns:
            batch.add_column(sa.Column("sandbox_image", sa.Text(), nullable=True))

        if "internal_port" in backend_columns and "handoff_port" not in backend_columns:
            batch.alter_column("internal_port", new_column_name="handoff_port")
        elif "handoff_port" not in backend_columns:
            batch.add_column(
                sa.Column(
                    "handoff_port", sa.Integer(), nullable=False, server_default="8000"
                )
            )

        for column_name in (
            "workdir",
            "install_command",
            "update_command",
            "start_command",
            "env_json",
        ):
            if column_name in backend_columns:
                batch.drop_column(column_name)


def downgrade() -> None:
    backend_columns = _column_names("backends")
    with op.batch_alter_table("backends") as batch:
        if "sandbox_image" in backend_columns and "base_image" not in backend_columns:
            batch.alter_column("sandbox_image", new_column_name="base_image")
        elif "base_image" not in backend_columns:
            batch.add_column(sa.Column("base_image", sa.Text(), nullable=True))

        if "handoff_port" in backend_columns and "internal_port" not in backend_columns:
            batch.alter_column("handoff_port", new_column_name="internal_port")
        elif "internal_port" not in backend_columns:
            batch.add_column(
                sa.Column(
                    "internal_port", sa.Integer(), nullable=False, server_default="8000"
                )
            )

        if "workdir" not in backend_columns:
            batch.add_column(sa.Column("workdir", sa.Text(), nullable=True))
        if "install_command" not in backend_columns:
            batch.add_column(sa.Column("install_command", sa.Text(), nullable=True))
        if "update_command" not in backend_columns:
            batch.add_column(sa.Column("update_command", sa.Text(), nullable=True))
        if "start_command" not in backend_columns:
            batch.add_column(sa.Column("start_command", sa.Text(), nullable=True))
        if "env_json" not in backend_columns:
            batch.add_column(
                sa.Column("env_json", sa.Text(), nullable=False, server_default="{}")
            )
