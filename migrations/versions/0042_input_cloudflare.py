"""Record domain certificate provider and managed Cloudflare DNS selection."""

from alembic import op
import sqlalchemy as sa

revision = "0042_input_cloudflare"
down_revision = "0041_input_https"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inputs",
        sa.Column(
            "https_provider",
            sa.String(32),
            nullable=False,
            server_default="self_signed",
        ),
    )
    op.add_column(
        "inputs",
        sa.Column(
            "cloudflare_dns_enabled", sa.Boolean(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "inputs", sa.Column("cloudflare_dns_ipv4", sa.String(15), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("inputs", "cloudflare_dns_ipv4")
    op.drop_column("inputs", "cloudflare_dns_enabled")
    op.drop_column("inputs", "https_provider")
