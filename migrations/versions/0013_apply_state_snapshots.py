"""persist apply desired state snapshots

Revision ID: 0013_apply_state_snapshots
Revises: 0012_operations
Create Date: 2026-04-27 18:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_apply_state_snapshots"
down_revision = "0012_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("apply_runs") as batch:
        batch.add_column(sa.Column("operation_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("config_revision", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column("desired_state_hash", sa.String(length=128), nullable=True)
        )
        batch.add_column(sa.Column("desired_state_json", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("generated_nginx_files_json", sa.Text(), nullable=True)
        )
        batch.add_column(sa.Column("runtime_graph_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("route_contracts_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("backend_contracts_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("resource_profile_json", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_apply_runs_operation_id_operations",
            "operations",
            ["operation_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_apply_runs_operation_id", ["operation_id"], unique=False)
        batch.create_index(
            "ix_apply_runs_desired_state_hash", ["desired_state_hash"], unique=False
        )
        batch.create_index(
            "ix_apply_runs_config_revision", ["config_revision"], unique=False
        )

    op.create_table(
        "host_apply_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("last_applied_state_hash", sa.String(length=128), nullable=True),
        sa.Column("last_successful_operation_id", sa.Integer(), nullable=True),
        sa.Column("last_successful_apply_run_id", sa.Integer(), nullable=True),
        sa.Column("last_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["last_successful_apply_run_id"], ["apply_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["last_successful_operation_id"], ["operations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("host_apply_state")
    with op.batch_alter_table("apply_runs") as batch:
        batch.drop_index("ix_apply_runs_config_revision")
        batch.drop_index("ix_apply_runs_desired_state_hash")
        batch.drop_index("ix_apply_runs_operation_id")
        batch.drop_constraint(
            "fk_apply_runs_operation_id_operations", type_="foreignkey"
        )
        batch.drop_column("resource_profile_json")
        batch.drop_column("backend_contracts_json")
        batch.drop_column("route_contracts_json")
        batch.drop_column("runtime_graph_json")
        batch.drop_column("generated_nginx_files_json")
        batch.drop_column("desired_state_json")
        batch.drop_column("desired_state_hash")
        batch.drop_column("config_revision")
        batch.drop_column("operation_id")
