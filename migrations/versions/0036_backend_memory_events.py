"""record backend memory event checkpoints

Revision ID: 0036_backend_memory_events
Revises: 0035_backend_ssh_keys
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa


revision = "0036_backend_memory_events"
down_revision = "0035_backend_ssh_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("backend_resource_samples") as batch:
        batch.add_column(sa.Column("memory_peak_bytes", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("memory_events_max", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("memory_events_oom_kill", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("backend_resource_samples") as batch:
        batch.drop_column("memory_events_oom_kill")
        batch.drop_column("memory_events_max")
        batch.drop_column("memory_peak_bytes")
