"""add backend placement node

Revision ID: 0028_backend_placement_node
Revises: 0027_backend_cpu_ceiling_samples
Create Date: 2026-05-16
"""

from alembic import op
import sqlalchemy as sa


revision = "0028_backend_placement_node"
down_revision = "0027_backend_cpu_ceiling_samples"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if "placement_node_uid" not in _columns("backends"):
        op.add_column(
            "backends",
            sa.Column("placement_node_uid", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    if "placement_node_uid" in _columns("backends"):
        op.drop_column("backends", "placement_node_uid")
