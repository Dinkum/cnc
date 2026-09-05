"""hard cut app backends to curated systemd guest sandboxes

Revision ID: 0010_systemd_guest_cutover
Revises: 0009_sandbox_handoff_cutover
Create Date: 2026-04-20 00:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_systemd_guest_cutover"
down_revision = "0009_sandbox_handoff_cutover"
branch_labels = None
depends_on = None

DEFAULT_SANDBOX_PROFILE = "ubuntu-24.04-systemd"


def _column_names(table_name: str) -> set[str]:
    return {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    backend_columns = _column_names("backends")
    with op.batch_alter_table("backends") as batch:
        if (
            "sandbox_image" in backend_columns
            and "sandbox_profile" not in backend_columns
        ):
            batch.alter_column("sandbox_image", new_column_name="sandbox_profile")
        elif "sandbox_profile" not in backend_columns:
            batch.add_column(sa.Column("sandbox_profile", sa.Text(), nullable=True))

        if "healthcheck_host_header" not in backend_columns:
            batch.add_column(
                sa.Column("healthcheck_host_header", sa.Text(), nullable=True)
            )

        for column_name in ("no_new_privileges", "drop_capabilities"):
            if column_name in backend_columns:
                batch.drop_column(column_name)

    op.execute(
        sa.text(
            "UPDATE backends "
            "SET sandbox_profile = :profile "
            "WHERE kind = 'app' AND COALESCE(TRIM(sandbox_profile), '') != ''"
        ).bindparams(profile=DEFAULT_SANDBOX_PROFILE)
    )
    op.execute(
        sa.text(
            "UPDATE backends "
            "SET sandbox_profile = :profile "
            "WHERE kind = 'app' AND COALESCE(TRIM(sandbox_profile), '') = ''"
        ).bindparams(profile=DEFAULT_SANDBOX_PROFILE)
    )


def downgrade() -> None:
    backend_columns = _column_names("backends")
    with op.batch_alter_table("backends") as batch:
        if (
            "sandbox_profile" in backend_columns
            and "sandbox_image" not in backend_columns
        ):
            batch.alter_column("sandbox_profile", new_column_name="sandbox_image")
        elif "sandbox_image" not in backend_columns:
            batch.add_column(sa.Column("sandbox_image", sa.Text(), nullable=True))

        if "no_new_privileges" not in backend_columns:
            batch.add_column(
                sa.Column(
                    "no_new_privileges",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("1"),
                )
            )
        if "drop_capabilities" not in backend_columns:
            batch.add_column(
                sa.Column(
                    "drop_capabilities",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("1"),
                )
            )
        if "healthcheck_host_header" in backend_columns:
            batch.drop_column("healthcheck_host_header")

    op.execute(
        sa.text(
            "UPDATE backends "
            "SET sandbox_image = 'docker.io/library/ubuntu:24.04' "
            "WHERE kind = 'app' AND COALESCE(TRIM(sandbox_image), '') = ''"
        )
    )
