"""add backend inter-app interface declarations

Revision ID: 0032_backend_inter_app_interfaces
Revises: 0031_backend_multi_node_placement
Create Date: 2026-05-17
"""

from alembic import op
import sqlalchemy as sa


revision = "0032_backend_inter_app_interfaces"
down_revision = "0031_backend_multi_node_placement"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    columns = _columns("backends")
    if "inter_app_interfaces_json" not in columns:
        op.add_column(
            "backends",
            sa.Column(
                "inter_app_interfaces_json",
                sa.Text(),
                nullable=False,
                server_default="[]",
            ),
        )


def downgrade() -> None:
    columns = _columns("backends")
    if "inter_app_interfaces_json" in columns:
        op.drop_column("backends", "inter_app_interfaces_json")
