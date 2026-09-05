"""drop stale backend backup snapshot-era columns

Revision ID: 0006_backend_backup_schema
Revises: 0005_backup_bundle
Create Date: 2026-03-30 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_backend_backup_schema"
down_revision = "0005_backup_bundle"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    return {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    columns = _column_names("backend_backups")
    if (
        "include_container_snapshot" not in columns
        and "container_image_ref" not in columns
    ):
        return

    op.create_table(
        "backend_backups__new",
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False
        ),
        sa.Column(
            "backend_id",
            sa.Integer(),
            sa.ForeignKey("backends.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("bundle_path", sa.Text(), nullable=True),
        sa.Column("bundle_sha256", sa.Text(), nullable=True),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO backend_backups__new (
                id, backend_id, status, scope, size_bytes, notes, error, created_at, bundle_path, bundle_sha256
            )
            SELECT
                id, backend_id, status, scope, size_bytes, notes, error, created_at, bundle_path, bundle_sha256
            FROM backend_backups
            """
        )
    )
    op.drop_table("backend_backups")
    op.rename_table("backend_backups__new", "backend_backups")


def downgrade() -> None:
    columns = _column_names("backend_backups")
    with op.batch_alter_table("backend_backups") as batch:
        if "include_container_snapshot" not in columns:
            batch.add_column(
                sa.Column(
                    "include_container_snapshot",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )
        if "container_image_ref" not in columns:
            batch.add_column(sa.Column("container_image_ref", sa.Text(), nullable=True))
