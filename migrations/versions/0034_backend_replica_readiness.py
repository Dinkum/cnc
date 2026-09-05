"""add backend replica readiness records

Revision ID: 0034_backend_replica_readiness
Revises: 0033_backend_backup_operation_id
Create Date: 2026-05-26
"""

from alembic import op
import sqlalchemy as sa


revision = "0034_backend_replica_readiness"
down_revision = "0033_backend_backup_operation_id"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return set(inspector.get_table_names())


def _indexes(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if "backend_replica_readiness" not in _tables():
        op.create_table(
            "backend_replica_readiness",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("backend_id", sa.Integer(), nullable=False),
            sa.Column("node_uid", sa.String(length=64), nullable=False),
            sa.Column("setup_mode", sa.String(length=32), nullable=False),
            sa.Column("runtime_contract_hash", sa.String(length=128), nullable=False),
            sa.Column("target_url", sa.Text(), nullable=False),
            sa.Column("healthcheck_result", sa.Text(), nullable=False),
            sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["backend_id"],
                ["backends.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "backend_id",
                "node_uid",
                name="uq_backend_replica_readiness_backend_node",
            ),
        )
    indexes = _indexes("backend_replica_readiness")
    if "ix_backend_replica_readiness_backend_id" not in indexes:
        op.create_index(
            "ix_backend_replica_readiness_backend_id",
            "backend_replica_readiness",
            ["backend_id"],
            unique=False,
        )
    if "ix_backend_replica_readiness_node_uid" not in indexes:
        op.create_index(
            "ix_backend_replica_readiness_node_uid",
            "backend_replica_readiness",
            ["node_uid"],
            unique=False,
        )


def downgrade() -> None:
    if "backend_replica_readiness" in _tables():
        indexes = _indexes("backend_replica_readiness")
        if "ix_backend_replica_readiness_node_uid" in indexes:
            op.drop_index(
                "ix_backend_replica_readiness_node_uid",
                table_name="backend_replica_readiness",
            )
        if "ix_backend_replica_readiness_backend_id" in indexes:
            op.drop_index(
                "ix_backend_replica_readiness_backend_id",
                table_name="backend_replica_readiness",
            )
        op.drop_table("backend_replica_readiness")
