"""add high level control events

Revision ID: 0011_control_events
Revises: 0010_systemd_guest_cutover
Create Date: 2026-04-21 15:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_control_events"
down_revision = "0010_systemd_guest_cutover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "control_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("backend_name", sa.String(length=255), nullable=True),
        sa.Column(
            "affects_all", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "related_backends_json",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "details_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "subevents_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("control_events") as batch:
        batch.create_index("ix_control_events_created_at", ["created_at"], unique=False)
        batch.create_index(
            "ix_control_events_backend_name", ["backend_name"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("control_events") as batch:
        batch.drop_index("ix_control_events_backend_name")
        batch.drop_index("ix_control_events_created_at")
    op.drop_table("control_events")
