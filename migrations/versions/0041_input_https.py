"""Add opt-in HTTPS to domain inputs."""

from alembic import op
import sqlalchemy as sa

revision = "0041_input_https"
down_revision = "0040_backend_ssh_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inputs",
        sa.Column("https_enabled", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("inputs", "https_enabled")
