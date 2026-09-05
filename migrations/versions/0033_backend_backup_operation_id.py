"""add backend backup operation correlation

Revision ID: 0033_backend_backup_operation_id
Revises: 0032_backend_inter_app_interfaces
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa


revision = "0033_backend_backup_operation_id"
down_revision = "0032_backend_inter_app_interfaces"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _has_operation_fk(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for foreign_key in inspector.get_foreign_keys(table_name):
        options = foreign_key.get("options")
        ondelete = ""
        if isinstance(options, dict):
            ondelete = str(options.get("ondelete") or "").upper()
        if (
            foreign_key.get("constrained_columns") == ["operation_id"]
            and foreign_key.get("referred_table") == "operations"
            and foreign_key.get("referred_columns") == ["id"]
            and ondelete == "SET NULL"
        ):
            return True
    return False


def upgrade() -> None:
    columns = _columns("backend_backups")
    needs_column = "operation_id" not in columns
    needs_foreign_key = needs_column or not _has_operation_fk("backend_backups")
    if needs_column or needs_foreign_key:
        with op.batch_alter_table("backend_backups", recreate="always") as batch_op:
            if needs_column:
                batch_op.add_column(
                    sa.Column("operation_id", sa.Integer(), nullable=True)
                )
            if needs_foreign_key:
                batch_op.create_foreign_key(
                    "fk_backend_backups_operation_id_operations",
                    "operations",
                    ["operation_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
    indexes = _indexes("backend_backups")
    if "ix_backend_backups_operation_id" not in indexes:
        op.create_index(
            "ix_backend_backups_operation_id",
            "backend_backups",
            ["operation_id"],
        )


def downgrade() -> None:
    indexes = _indexes("backend_backups")
    if "ix_backend_backups_operation_id" in indexes:
        op.drop_index("ix_backend_backups_operation_id", table_name="backend_backups")
    columns = _columns("backend_backups")
    if "operation_id" in columns:
        with op.batch_alter_table("backend_backups", recreate="always") as batch_op:
            batch_op.drop_column("operation_id")
