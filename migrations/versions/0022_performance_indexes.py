"""add performance indexes

Revision ID: 0022_performance_indexes
Revises: 0021_output_disk_metric_samples
Create Date: 2026-05-15 00:00:00
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect


revision = "0022_performance_indexes"
down_revision = "0021_output_disk_metric_samples"
branch_labels = None
depends_on = None


def _create_index_if_missing(
    index_name: str, table_name: str, columns: list[str]
) -> None:
    bind = op.get_bind()
    existing = {index["name"] for index in inspect(bind).get_indexes(table_name)}
    if index_name in existing:
        return
    op.create_index(index_name, table_name, columns)


def _drop_index_if_present(index_name: str, table_name: str) -> None:
    bind = op.get_bind()
    existing = {index["name"] for index in inspect(bind).get_indexes(table_name)}
    if index_name not in existing:
        return
    op.drop_index(index_name, table_name=table_name)


def upgrade() -> None:
    _create_index_if_missing(
        "ix_backend_resource_samples_bucket_start",
        "backend_resource_samples",
        ["bucket_start"],
    )
    _create_index_if_missing(
        "ix_host_resource_samples_bucket_start",
        "host_resource_samples",
        ["bucket_start"],
    )
    _create_index_if_missing(
        "ix_control_events_kind_created_at", "control_events", ["kind", "created_at"]
    )


def downgrade() -> None:
    _drop_index_if_present("ix_control_events_kind_created_at", "control_events")
    _drop_index_if_present(
        "ix_host_resource_samples_bucket_start", "host_resource_samples"
    )
    _drop_index_if_present(
        "ix_backend_resource_samples_bucket_start", "backend_resource_samples"
    )
