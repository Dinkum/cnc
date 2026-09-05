"""add metric chart accuracy fields

Revision ID: 0030_metric_chart_accuracy
Revises: 0029_hot_path_indexes
Create Date: 2026-05-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0030_metric_chart_accuracy"
down_revision = "0029_hot_path_indexes"
branch_labels = None
depends_on = None


def _add_rollup_columns(batch: op.BatchOperations, prefix: str) -> None:
    batch.add_column(
        sa.Column(
            f"{prefix}_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        )
    )
    batch.add_column(sa.Column(f"{prefix}_sum", sa.Float(), nullable=True))
    batch.add_column(sa.Column(f"{prefix}_min", sa.Float(), nullable=True))
    batch.add_column(sa.Column(f"{prefix}_max", sa.Float(), nullable=True))
    batch.add_column(sa.Column(f"{prefix}_first", sa.Float(), nullable=True))
    batch.add_column(sa.Column(f"{prefix}_last", sa.Float(), nullable=True))


def _drop_rollup_columns(batch: op.BatchOperations, prefix: str) -> None:
    batch.drop_column(f"{prefix}_last")
    batch.drop_column(f"{prefix}_first")
    batch.drop_column(f"{prefix}_max")
    batch.drop_column(f"{prefix}_min")
    batch.drop_column(f"{prefix}_sum")
    batch.drop_column(f"{prefix}_count")


def upgrade() -> None:
    with op.batch_alter_table("backend_resource_samples") as batch:
        batch.add_column(
            sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("cpu_limit_percent_of_host", sa.Float(), nullable=True)
        )
        batch.add_column(sa.Column("disk_usage_complete", sa.Boolean(), nullable=True))
        batch.add_column(
            sa.Column("disk_usage_skipped_paths", sa.Integer(), nullable=True)
        )
        batch.add_column(sa.Column("network_rx_bps", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("network_tx_bps", sa.Integer(), nullable=True))
        _add_rollup_columns(batch, "memory_current_bytes")
        _add_rollup_columns(batch, "network_rx_bps")
        _add_rollup_columns(batch, "network_tx_bps")

    with op.batch_alter_table("host_resource_samples") as batch:
        batch.add_column(
            sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=True)
        )
        _add_rollup_columns(batch, "network_rx_bps")
        _add_rollup_columns(batch, "network_tx_bps")

    op.execute(
        sa.text(
            """
            UPDATE backend_resource_samples
            SET
                sampled_at = bucket_start,
                cpu_limit_percent_of_host = cpu_entitlement_percent_of_host,
                memory_current_bytes_count = CASE WHEN memory_current_bytes IS NULL THEN 0 ELSE 1 END,
                memory_current_bytes_sum = memory_current_bytes,
                memory_current_bytes_min = memory_current_bytes,
                memory_current_bytes_max = memory_current_bytes,
                memory_current_bytes_first = memory_current_bytes,
                memory_current_bytes_last = memory_current_bytes
            """
        )
    )
    op.execute(sa.text("UPDATE host_resource_samples SET sampled_at = bucket_start"))


def downgrade() -> None:
    with op.batch_alter_table("host_resource_samples") as batch:
        _drop_rollup_columns(batch, "network_tx_bps")
        _drop_rollup_columns(batch, "network_rx_bps")
        batch.drop_column("sampled_at")

    with op.batch_alter_table("backend_resource_samples") as batch:
        _drop_rollup_columns(batch, "network_tx_bps")
        _drop_rollup_columns(batch, "network_rx_bps")
        _drop_rollup_columns(batch, "memory_current_bytes")
        batch.drop_column("network_tx_bps")
        batch.drop_column("network_rx_bps")
        batch.drop_column("disk_usage_skipped_paths")
        batch.drop_column("disk_usage_complete")
        batch.drop_column("cpu_limit_percent_of_host")
        batch.drop_column("sampled_at")
