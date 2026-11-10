"""Choose a domain's connection independently of its DNS manager."""

from alembic import op
import sqlalchemy as sa

revision = "0044_input_connection"
down_revision = "0043_host_journal_commits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inputs",
        sa.Column(
            "connection", sa.String(32), nullable=False, server_default="public_ip"
        ),
    )


def downgrade() -> None:
    op.drop_column("inputs", "connection")
