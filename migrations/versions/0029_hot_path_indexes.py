"""add hot path indexes

Revision ID: 0029_hot_path_indexes
Revises: 0028_backend_placement_node
Create Date: 2026-05-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0029_hot_path_indexes"
down_revision = "0028_backend_placement_node"
branch_labels = None
depends_on = None


def _create_index_if_missing(
    index_name: str, table_name: str, columns: list[str]
) -> None:
    bind = op.get_bind()
    existing = {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}
    if index_name in existing:
        return
    op.create_index(index_name, table_name, columns)


def _drop_index_if_present(index_name: str, table_name: str) -> None:
    bind = op.get_bind()
    existing = {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}
    if index_name not in existing:
        return
    op.drop_index(index_name, table_name=table_name)


def upgrade() -> None:
    _create_index_if_missing("ix_apply_runs_status_id", "apply_runs", ["status", "id"])
    _create_index_if_missing(
        "ix_backend_backups_backend_id_id",
        "backend_backups",
        ["backend_id", "id"],
    )
    _create_index_if_missing(
        "ix_backend_backups_backend_id_status_id",
        "backend_backups",
        ["backend_id", "status", "id"],
    )


def downgrade() -> None:
    _drop_index_if_present("ix_backend_backups_backend_id_status_id", "backend_backups")
    _drop_index_if_present("ix_backend_backups_backend_id_id", "backend_backups")
    _drop_index_if_present("ix_apply_runs_status_id", "apply_runs")
