"""add mutable app backend fields

Revision ID: 0003_app_backends
Revises: 0002_update_runs
Create Date: 2026-03-25 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0003_app_backends"
down_revision = "0002_update_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("backends", sa.Column("base_image", sa.Text(), nullable=True))
    op.add_column("backends", sa.Column("workdir", sa.Text(), nullable=True))
    op.add_column("backends", sa.Column("install_command", sa.Text(), nullable=True))
    op.add_column("backends", sa.Column("start_command", sa.Text(), nullable=True))
    op.add_column("backends", sa.Column("update_command", sa.Text(), nullable=True))
    op.add_column("backends", sa.Column("healthcheck_path", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("backends", "healthcheck_path")
    op.drop_column("backends", "update_command")
    op.drop_column("backends", "start_command")
    op.drop_column("backends", "install_command")
    op.drop_column("backends", "workdir")
    op.drop_column("backends", "base_image")
