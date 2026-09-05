"""add output network metric samples

Revision ID: 0016_output_network_metrics
Revises: 0015_resource_sample_rollups
Create Date: 2026-04-30 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0016_output_network_metrics"
down_revision = "0015_resource_sample_rollups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("backend_resource_samples") as batch:
        batch.add_column(sa.Column("network_rx_bytes", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("network_tx_bytes", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("network_total_bps", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "network_total_bps_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(sa.Column("network_total_bps_sum", sa.Float(), nullable=True))
        batch.add_column(sa.Column("network_total_bps_min", sa.Float(), nullable=True))
        batch.add_column(sa.Column("network_total_bps_max", sa.Float(), nullable=True))
        batch.add_column(
            sa.Column("network_total_bps_first", sa.Float(), nullable=True)
        )
        batch.add_column(sa.Column("network_total_bps_last", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("backend_resource_samples") as batch:
        batch.drop_column("network_total_bps_last")
        batch.drop_column("network_total_bps_first")
        batch.drop_column("network_total_bps_max")
        batch.drop_column("network_total_bps_min")
        batch.drop_column("network_total_bps_sum")
        batch.drop_column("network_total_bps_count")
        batch.drop_column("network_total_bps")
        batch.drop_column("network_tx_bytes")
        batch.drop_column("network_rx_bytes")
