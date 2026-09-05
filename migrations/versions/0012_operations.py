"""add durable host mutation operations

Revision ID: 0012_operations
Revises: 0011_control_events
Create Date: 2026-04-27 16:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0012_operations"
down_revision = "0011_control_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("phase", sa.String(length=64), nullable=True),
        sa.Column("actor", sa.String(length=64), nullable=True),
        sa.Column("backend_id", sa.Integer(), nullable=True),
        sa.Column("config_revision", sa.String(length=64), nullable=True),
        sa.Column("desired_state_hash", sa.String(length=128), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "details_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.ForeignKeyConstraint(["backend_id"], ["backends.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("operations") as batch:
        batch.create_index("ix_operations_started_at", ["started_at"], unique=False)
        batch.create_index("ix_operations_kind", ["kind"], unique=False)
        batch.create_index("ix_operations_status", ["status"], unique=False)
        batch.create_index("ix_operations_backend_id", ["backend_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("operations") as batch:
        batch.drop_index("ix_operations_backend_id")
        batch.drop_index("ix_operations_status")
        batch.drop_index("ix_operations_kind")
        batch.drop_index("ix_operations_started_at")
    op.drop_table("operations")
