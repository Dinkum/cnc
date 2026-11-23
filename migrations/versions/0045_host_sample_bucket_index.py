"""Remove the index already covered by the host sample bucket constraint."""

from alembic import op


revision = "0045_host_sample_bucket_index"
down_revision = "0044_input_connection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_host_resource_samples_bucket_start", table_name="host_resource_samples"
    )


def downgrade() -> None:
    op.create_index(
        "ix_host_resource_samples_bucket_start",
        "host_resource_samples",
        ["bucket_start"],
    )
