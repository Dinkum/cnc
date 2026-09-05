"""cut backend backups over to single bundle archives

Revision ID: 0005_backup_bundle
Revises: 0004_current_schema
Create Date: 2026-03-30 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_backup_bundle"
down_revision = "0004_current_schema"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    return {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    columns = _column_names("backend_backups")
    with op.batch_alter_table("backend_backups") as batch:
        if "bundle_path" not in columns:
            batch.add_column(sa.Column("bundle_path", sa.Text(), nullable=True))
        if "bundle_sha256" not in columns:
            batch.add_column(sa.Column("bundle_sha256", sa.Text(), nullable=True))
        if "data_archive_path" in columns:
            batch.drop_column("data_archive_path")
        if "metadata_path" in columns:
            batch.drop_column("metadata_path")


def downgrade() -> None:
    columns = _column_names("backend_backups")
    with op.batch_alter_table("backend_backups") as batch:
        if "metadata_path" not in columns:
            batch.add_column(sa.Column("metadata_path", sa.Text(), nullable=True))
        if "data_archive_path" not in columns:
            batch.add_column(sa.Column("data_archive_path", sa.Text(), nullable=True))
        if "bundle_sha256" in columns:
            batch.drop_column("bundle_sha256")
        if "bundle_path" in columns:
            batch.drop_column("bundle_path")
