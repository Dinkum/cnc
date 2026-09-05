"""add output disk metric samples

Revision ID: 0021_output_disk_metric_samples
Revises: 0020_input_shield_access_gate
Create Date: 2026-05-15 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0021_output_disk_metric_samples"
down_revision = "0020_input_shield_access_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("backend_resource_samples") as batch:
        batch.add_column(sa.Column("disk_usage_bytes", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "disk_usage_bytes_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(sa.Column("disk_usage_bytes_sum", sa.Float(), nullable=True))
        batch.add_column(sa.Column("disk_usage_bytes_min", sa.Float(), nullable=True))
        batch.add_column(sa.Column("disk_usage_bytes_max", sa.Float(), nullable=True))
        batch.add_column(sa.Column("disk_usage_bytes_first", sa.Float(), nullable=True))
        batch.add_column(sa.Column("disk_usage_bytes_last", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("backend_resource_samples") as batch:
        batch.drop_column("disk_usage_bytes_last")
        batch.drop_column("disk_usage_bytes_first")
        batch.drop_column("disk_usage_bytes_max")
        batch.drop_column("disk_usage_bytes_min")
        batch.drop_column("disk_usage_bytes_sum")
        batch.drop_column("disk_usage_bytes_count")
        batch.drop_column("disk_usage_bytes")
