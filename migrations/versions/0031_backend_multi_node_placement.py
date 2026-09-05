"""add backend multi-node placement fields

Revision ID: 0031_backend_multi_node_placement
Revises: 0030_metric_chart_accuracy
Create Date: 2026-05-17
"""

from alembic import op
import sqlalchemy as sa


revision = "0031_backend_multi_node_placement"
down_revision = "0030_metric_chart_accuracy"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    columns = _columns("backends")
    if "placement_mode" not in columns:
        op.add_column(
            "backends",
            sa.Column(
                "placement_mode",
                sa.String(length=16),
                nullable=False,
                server_default="single",
            ),
        )
    if "placement_active_node_uid" not in columns:
        op.add_column(
            "backends",
            sa.Column("placement_active_node_uid", sa.String(length=64), nullable=True),
        )
    if "placement_node_uids_json" not in columns:
        op.add_column(
            "backends",
            sa.Column(
                "placement_node_uids_json",
                sa.Text(),
                nullable=False,
                server_default="[]",
            ),
        )

    op.execute(
        sa.text(
            """
            UPDATE backends
            SET placement_active_node_uid = COALESCE(NULLIF(placement_node_uid, ''), 'local')
            WHERE placement_active_node_uid IS NULL OR placement_active_node_uid = ''
            """
        )
    )


def downgrade() -> None:
    columns = _columns("backends")
    if "placement_node_uids_json" in columns:
        op.drop_column("backends", "placement_node_uids_json")
    if "placement_active_node_uid" in columns:
        op.drop_column("backends", "placement_active_node_uid")
    if "placement_mode" in columns:
        op.drop_column("backends", "placement_mode")
