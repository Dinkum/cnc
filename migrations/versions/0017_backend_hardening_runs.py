"""add backend hardening runs

Revision ID: 0017_backend_hardening_runs
Revises: 0016_output_network_metrics
Create Date: 2026-05-01 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0017_backend_hardening_runs"
down_revision = "0016_output_network_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backend_hardening_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "backend_id",
            sa.Integer(),
            sa.ForeignKey("backends.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="queued"
        ),
        sa.Column("evidence_dir", sa.Text(), nullable=True),
        sa.Column("ratings_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_backend_hardening_runs_backend_phase_started",
        "backend_hardening_runs",
        ["backend_id", "phase", "started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_backend_hardening_runs_backend_phase_started",
        table_name="backend_hardening_runs",
    )
    op.drop_table("backend_hardening_runs")
