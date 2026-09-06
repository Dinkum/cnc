"""persist reviewed app hardening configuration

Revision ID: 0038_backend_hardening_config
Revises: 0037_control_event_target_indexes
"""

from alembic import op
import sqlalchemy as sa

revision = "0038_backend_hardening_config"
down_revision = "0037_control_event_target_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backends",
        sa.Column(
            "hardening_config_json", sa.Text(), nullable=False, server_default="{}"
        ),
    )
    op.add_column(
        "backends", sa.Column("hardening_previous_json", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("backends", "hardening_previous_json")
    op.drop_column("backends", "hardening_config_json")
