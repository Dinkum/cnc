"""hard cutover to current schema

Revision ID: 0004_current_schema
Revises: 0003_app_backends
Create Date: 2026-03-29 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_current_schema"
down_revision = "0003_app_backends"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    return {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "input_backend_links" not in tables:
        op.create_table(
            "input_backend_links",
            sa.Column("input_id", sa.Integer(), nullable=False),
            sa.Column("backend_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["backend_id"], ["backends.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["input_id"], ["inputs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("input_id", "backend_id"),
        )

    backend_columns = _column_names("backends")
    with op.batch_alter_table("backends") as batch:
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
        if "healthcheck_mode" not in backend_columns:
            batch.add_column(sa.Column("healthcheck_mode", sa.Text(), nullable=True))
        if "memory_high_override" not in backend_columns:
            batch.add_column(
                sa.Column("memory_high_override", sa.Text(), nullable=True)
            )
        if "memory_max_override" not in backend_columns:
            batch.add_column(sa.Column("memory_max_override", sa.Text(), nullable=True))
        if "cpu_quota_override" not in backend_columns:
            batch.add_column(sa.Column("cpu_quota_override", sa.Text(), nullable=True))
        if "image" in backend_columns:
            batch.drop_column("image")
        if "update_command" in backend_columns:
            batch.drop_column("update_command")

    input_columns = _column_names("inputs")
    if "backend_id" in input_columns:
        op.execute(
            sa.text(
                """
                INSERT OR IGNORE INTO input_backend_links (input_id, backend_id)
                SELECT id, backend_id
                FROM inputs
                WHERE backend_id IS NOT NULL
                """
            )
        )
    with op.batch_alter_table("inputs") as batch:
        if "kind" not in input_columns:
            batch.add_column(
                sa.Column(
                    "kind",
                    sa.String(length=32),
                    nullable=False,
                    server_default=sa.text("'domain'"),
                )
            )
        if "backend_id" in input_columns:
            batch.drop_column("backend_id")

    if "backend_backups" not in tables:
        op.create_table(
            "backend_backups",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("backend_id", sa.Integer(), nullable=True),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'running'"),
            ),
            sa.Column(
                "scope",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'metadata_only'"),
            ),
            sa.Column("data_archive_path", sa.Text(), nullable=True),
            sa.Column("metadata_path", sa.Text(), nullable=True),
            sa.Column("size_bytes", sa.Integer(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["backend_id"], ["backends.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    with op.batch_alter_table("inputs") as batch:
        batch.add_column(sa.Column("backend_id", sa.Integer(), nullable=True))
        batch.drop_column("kind")

    op.execute(
        sa.text(
            """
            UPDATE inputs
            SET backend_id = (
                SELECT backend_id
                FROM input_backend_links
                WHERE input_backend_links.input_id = inputs.id
                ORDER BY backend_id
                LIMIT 1
            )
            """
        )
    )

    with op.batch_alter_table("backends") as batch:
        batch.add_column(sa.Column("image", sa.Text(), nullable=True))
        batch.add_column(sa.Column("update_command", sa.Text(), nullable=True))
        batch.drop_column("cpu_quota_override")
        batch.drop_column("memory_max_override")
        batch.drop_column("memory_high_override")
        batch.drop_column("healthcheck_mode")
        batch.drop_column("drop_capabilities")
        batch.drop_column("no_new_privileges")

    op.drop_table("backend_backups")
    op.drop_table("input_backend_links")
