"""Persist background command identity and execution ownership.

Revision ID: 0039_command_jobs
Revises: 0038_backend_hardening_config
"""

from alembic import op
import sqlalchemy as sa

revision = "0039_command_jobs"
down_revision = "0038_backend_hardening_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "command_jobs",
        sa.Column(
            "operation_id",
            sa.Integer(),
            sa.ForeignKey("operations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("execution_token", sa.String(32), nullable=False, unique=True),
        sa.Column("request_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("output_name", sa.String(255), nullable=False),
        sa.Column("container_id", sa.String(128), nullable=False),
        sa.Column("container_started_at", sa.String(64), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("command_jobs")
