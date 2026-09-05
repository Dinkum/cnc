"""persist update check results

Revision ID: 0014_update_checks
Revises: 0013_apply_state_snapshots
Create Date: 2026-04-27 19:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0014_update_checks"
down_revision = "0013_apply_state_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "update_checks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("current_version", sa.String(length=64), nullable=False),
        sa.Column("available_version", sa.String(length=64), nullable=True),
        sa.Column(
            "has_update", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "details_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_update_checks_checked_at", "update_checks", ["checked_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_update_checks_checked_at", table_name="update_checks")
    op.drop_table("update_checks")
