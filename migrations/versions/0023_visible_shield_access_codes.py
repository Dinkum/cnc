"""store visible Shield access codes

Revision ID: 0023_visible_shield_access_codes
Revises: 0022_performance_indexes
Create Date: 2026-05-15 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0023_visible_shield_access_codes"
down_revision = "0022_performance_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("backends", sa.Column("shield_access_code", sa.Text(), nullable=True))
    op.add_column("inputs", sa.Column("shield_access_code", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("inputs", "shield_access_code")
    op.drop_column("backends", "shield_access_code")
