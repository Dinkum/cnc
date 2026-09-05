"""add cluster node join state

Revision ID: 0024_cluster_nodes
Revises: 0023_visible_shield_access_codes
Create Date: 2026-05-16 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0024_cluster_nodes"
down_revision = "0023_visible_shield_access_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cluster_nodes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("node_uid", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "role", sa.String(length=32), server_default="follower", nullable=False
        ),
        sa.Column(
            "state", sa.String(length=32), server_default="joining", nullable=False
        ),
        sa.Column("wireguard_ip", sa.String(length=64), nullable=False),
        sa.Column("wireguard_public_key", sa.Text(), nullable=False),
        sa.Column("public_endpoint", sa.Text(), nullable=True),
        sa.Column("ram_bytes", sa.Integer(), nullable=True),
        sa.Column("cpu_count", sa.Integer(), nullable=True),
        sa.Column("disk_bytes", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_uid", name="uq_cluster_nodes_node_uid"),
    )
    op.create_index(
        "ix_cluster_nodes_state_last_seen", "cluster_nodes", ["state", "last_seen_at"]
    )
    op.create_table(
        "cluster_join_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("node_uid", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_cluster_join_tokens_token_hash"),
    )
    op.create_index(
        "ix_cluster_join_tokens_expires_at", "cluster_join_tokens", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_cluster_join_tokens_expires_at", table_name="cluster_join_tokens")
    op.drop_table("cluster_join_tokens")
    op.drop_index("ix_cluster_nodes_state_last_seen", table_name="cluster_nodes")
    op.drop_table("cluster_nodes")
