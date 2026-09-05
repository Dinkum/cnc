"""add host resource metric samples

Revision ID: 0019_host_resource_samples
Revises: 0018_shield_access_gate
Create Date: 2026-05-02 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0019_host_resource_samples"
down_revision = "0018_shield_access_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "host_resource_samples",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cpu_percent", sa.Integer(), nullable=True),
        sa.Column("memory_percent", sa.Integer(), nullable=True),
        sa.Column("disk_percent", sa.Integer(), nullable=True),
        sa.Column("network_rx_bytes", sa.Integer(), nullable=True),
        sa.Column("network_tx_bytes", sa.Integer(), nullable=True),
        sa.Column("network_rx_bps", sa.Integer(), nullable=True),
        sa.Column("network_tx_bps", sa.Integer(), nullable=True),
        sa.Column("network_total_bps", sa.Integer(), nullable=True),
        sa.Column("cpu_total_jiffies", sa.Integer(), nullable=True),
        sa.Column("cpu_idle_jiffies", sa.Integer(), nullable=True),
        sa.Column(
            "cpu_percent_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("cpu_percent_sum", sa.Float(), nullable=True),
        sa.Column("cpu_percent_min", sa.Float(), nullable=True),
        sa.Column("cpu_percent_max", sa.Float(), nullable=True),
        sa.Column("cpu_percent_first", sa.Float(), nullable=True),
        sa.Column("cpu_percent_last", sa.Float(), nullable=True),
        sa.Column(
            "memory_percent_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("memory_percent_sum", sa.Float(), nullable=True),
        sa.Column("memory_percent_min", sa.Float(), nullable=True),
        sa.Column("memory_percent_max", sa.Float(), nullable=True),
        sa.Column("memory_percent_first", sa.Float(), nullable=True),
        sa.Column("memory_percent_last", sa.Float(), nullable=True),
        sa.Column(
            "disk_percent_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("disk_percent_sum", sa.Float(), nullable=True),
        sa.Column("disk_percent_min", sa.Float(), nullable=True),
        sa.Column("disk_percent_max", sa.Float(), nullable=True),
        sa.Column("disk_percent_first", sa.Float(), nullable=True),
        sa.Column("disk_percent_last", sa.Float(), nullable=True),
        sa.Column(
            "network_total_bps_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("network_total_bps_sum", sa.Float(), nullable=True),
        sa.Column("network_total_bps_min", sa.Float(), nullable=True),
        sa.Column("network_total_bps_max", sa.Float(), nullable=True),
        sa.Column("network_total_bps_first", sa.Float(), nullable=True),
        sa.Column("network_total_bps_last", sa.Float(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.UniqueConstraint("bucket_start", name="uq_host_resource_sample_bucket"),
    )


def downgrade() -> None:
    op.drop_table("host_resource_samples")
