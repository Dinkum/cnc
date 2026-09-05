"""add shield access gate fields

Revision ID: 0018_shield_access_gate
Revises: 0017_backend_hardening_runs
Create Date: 2026-05-01 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0018_shield_access_gate"
down_revision = "0017_backend_hardening_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backends",
        sa.Column(
            "shield_enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column("backends", sa.Column("shield_code_hash", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("backends", "shield_code_hash")
    op.drop_column("backends", "shield_enabled")
