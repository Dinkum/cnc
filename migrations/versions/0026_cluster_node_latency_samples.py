"""add cluster node latency samples

Revision ID: 0026_cluster_node_latency_samples
Revises: 0025_cluster_node_tailnet_ip
Create Date: 2026-05-16 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0026_cluster_node_latency_samples"
down_revision = "0025_cluster_node_tailnet_ip"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cluster_node_latency_samples",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("node_uid", sa.String(length=64), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cluster_node_latency_samples_node_recorded",
        "cluster_node_latency_samples",
        ["node_uid", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cluster_node_latency_samples_node_recorded",
        table_name="cluster_node_latency_samples",
    )
    op.drop_table("cluster_node_latency_samples")
