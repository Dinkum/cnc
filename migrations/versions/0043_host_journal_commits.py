"""Record which host journals reached a durable database commit."""

from alembic import op
import sqlalchemy as sa

revision = "0043_host_journal_commits"
down_revision = "0042_input_cloudflare"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "host_journal_commits",
        sa.Column("journal_id", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("host_journal_commits")
