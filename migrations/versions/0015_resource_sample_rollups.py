"""add resource sample rollup fields

Revision ID: 0015_resource_sample_rollups
Revises: 0014_update_checks
Create Date: 2026-04-29 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0015_resource_sample_rollups"
down_revision = "0014_update_checks"
branch_labels = None
depends_on = None


ROLLUP_METRICS = (
    ("cpu_percent_of_host", "cpu_percent_of_host"),
    ("cpu_percent_of_entitlement", "cpu_percent_of_entitlement"),
    ("memory_percent", "memory_percent"),
)


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
        for prefix, _legacy_column in ROLLUP_METRICS:
            _add_rollup_columns(batch, prefix)

    for prefix, legacy_column in ROLLUP_METRICS:
        op.execute(
            sa.text(
                f"""
                UPDATE backend_resource_samples
                SET
                    {prefix}_count = CASE WHEN {legacy_column} IS NULL THEN 0 ELSE 1 END,
                    {prefix}_sum = {legacy_column},
                    {prefix}_min = {legacy_column},
                    {prefix}_max = {legacy_column},
                    {prefix}_first = {legacy_column},
                    {prefix}_last = {legacy_column}
                """
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("backend_resource_samples") as batch:
        for prefix, _legacy_column in reversed(ROLLUP_METRICS):
            _drop_rollup_columns(batch, prefix)
