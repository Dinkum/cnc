"""store backend cpu ceiling samples

Revision ID: 0027_backend_cpu_ceiling_samples
Revises: 0026_cluster_node_latency_samples
Create Date: 2026-05-16 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0027_backend_cpu_ceiling_samples"
down_revision = "0026_cluster_node_latency_samples"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    if "cpu_entitlement_percent_of_host" not in _columns("backend_resource_samples"):
        op.add_column(
            "backend_resource_samples",
            sa.Column("cpu_entitlement_percent_of_host", sa.Float(), nullable=True),
        )


def downgrade() -> None:
    if "cpu_entitlement_percent_of_host" in _columns("backend_resource_samples"):
        op.drop_column("backend_resource_samples", "cpu_entitlement_percent_of_host")
