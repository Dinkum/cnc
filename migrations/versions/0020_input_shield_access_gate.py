"""add input shield access gate fields

Revision ID: 0020_input_shield_access_gate
Revises: 0019_host_resource_samples
Create Date: 2026-05-15 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0020_input_shield_access_gate"
down_revision = "0019_host_resource_samples"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inputs",
        sa.Column(
            "shield_enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column("inputs", sa.Column("shield_code_hash", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("inputs", "shield_code_hash")
    op.drop_column("inputs", "shield_enabled")
