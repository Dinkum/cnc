"""index output-scoped control event queries

Revision ID: 0037_control_event_target_indexes
Revises: 0036_backend_memory_events
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0037_control_event_target_indexes"
down_revision = "0036_backend_memory_events"
branch_labels = None
depends_on = None


def _index_names() -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes("control_events")
    }


def upgrade() -> None:
    index_names = _index_names()
    if "ix_control_events_backend_name" in index_names:
        op.drop_index("ix_control_events_backend_name", table_name="control_events")
    if "ix_control_events_backend_name_id" not in index_names:
        op.create_index(
            "ix_control_events_backend_name_id",
            "control_events",
            ["backend_name", "id"],
        )
    if "ix_control_events_scope_affects_all_id" not in index_names:
        op.create_index(
            "ix_control_events_scope_affects_all_id",
            "control_events",
            ["scope", "affects_all", "id"],
        )


def downgrade() -> None:
    index_names = _index_names()
    if "ix_control_events_scope_affects_all_id" in index_names:
        op.drop_index(
            "ix_control_events_scope_affects_all_id", table_name="control_events"
        )
    if "ix_control_events_backend_name_id" in index_names:
        op.drop_index("ix_control_events_backend_name_id", table_name="control_events")
    if "ix_control_events_backend_name" not in index_names:
        op.create_index(
            "ix_control_events_backend_name",
            "control_events",
            ["backend_name"],
        )
