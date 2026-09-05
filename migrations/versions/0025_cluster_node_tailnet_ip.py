"""add cluster node tailnet address

Revision ID: 0025_cluster_node_tailnet_ip
Revises: 0024_cluster_nodes
Create Date: 2026-05-16 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0025_cluster_node_tailnet_ip"
down_revision = "0024_cluster_nodes"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    if "tailnet_ip" not in _columns("cluster_nodes"):
        op.add_column(
            "cluster_nodes",
            sa.Column("tailnet_ip", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    if "tailnet_ip" in _columns("cluster_nodes"):
        op.drop_column("cluster_nodes", "tailnet_ip")
