"""add backend auto-size policy fields and resource sample history

Revision ID: 0007_auto_size_resource_policy
Revises: 0006_backend_backup_schema
Create Date: 2026-04-01 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007_auto_size_resource_policy"
down_revision = "0006_backend_backup_schema"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    return {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    backend_columns = _column_names("backends")
    with op.batch_alter_table("backends") as batch:
        if "resource_mode" not in backend_columns:
            batch.add_column(
                sa.Column(
                    "resource_mode",
                    sa.String(length=16),
                    nullable=False,
                    server_default="auto",
                )
            )
        if "resource_size" not in backend_columns:
            batch.add_column(
                sa.Column(
                    "resource_size",
                    sa.String(length=16),
                    nullable=False,
                    server_default="small",
                )
            )
        if "resource_size_updated_at" not in backend_columns:
            batch.add_column(
                sa.Column(
                    "resource_size_updated_at",
                    sa.DateTime(timezone=True),
                    nullable=True,
                )
            )

    op.execute(
        sa.text("UPDATE backends SET resource_mode = COALESCE(resource_mode, 'auto')")
    )
    op.execute(
        sa.text("UPDATE backends SET resource_size = COALESCE(resource_size, 'small')")
    )

    if "backend_resource_samples" not in _table_names():
        op.create_table(
            "backend_resource_samples",
            sa.Column(
                "id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False
            ),
            sa.Column(
                "backend_id",
                sa.Integer(),
                sa.ForeignKey("backends.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("cpu_percent_of_host", sa.Integer(), nullable=True),
            sa.Column("cpu_percent_of_entitlement", sa.Integer(), nullable=True),
            sa.Column("memory_percent", sa.Integer(), nullable=True),
            sa.Column("memory_current_bytes", sa.Integer(), nullable=True),
            sa.Column("memory_max_bytes", sa.Integer(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "backend_id", "bucket_start", name="uq_backend_resource_sample_bucket"
            ),
        )


def downgrade() -> None:
    if "backend_resource_samples" in _table_names():
        op.drop_table("backend_resource_samples")

    backend_columns = _column_names("backends")
    with op.batch_alter_table("backends") as batch:
        if "resource_size_updated_at" in backend_columns:
            batch.drop_column("resource_size_updated_at")
        if "resource_size" in backend_columns:
            batch.drop_column("resource_size")
        if "resource_mode" in backend_columns:
            batch.drop_column("resource_mode")
