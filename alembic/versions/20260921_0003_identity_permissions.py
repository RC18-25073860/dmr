"""Add local identity and permission tables

Revision ID: 20260921_0003
Revises: 20260921_0002
Create Date: 2026-09-21
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260921_0003"
down_revision = "20260921_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("batches", sa.Column("owner_user_id", sa.String(), nullable=True))
    op.create_index("idx_batches_owner", "batches", ["owner_user_id"])
    op.create_table(
        "identity_users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("username_normalized", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("password_changed_at", sa.String(), nullable=False),
        sa.Column("can_download", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("can_history", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("identity_source", sa.String(), nullable=False, server_default="local"),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_index("uq_identity_users_username_normalized", "identity_users", ["username_normalized"], unique=True)
    op.create_index("idx_identity_users_role_active", "identity_users", ["role", "active"])
    op.create_table(
        "identity_scopes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("identity_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("registrant", sa.String(), nullable=False),
        sa.Column("project_category", sa.String(), nullable=False),
        sa.Column("product", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index("idx_identity_scopes_user", "identity_scopes", ["user_id"])
    op.create_table(
        "identity_sessions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("identity_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("last_seen_at", sa.String(), nullable=False),
        sa.Column("revoked_at", sa.String(), nullable=True),
    )
    op.create_index("uq_identity_sessions_token_hash", "identity_sessions", ["token_hash"], unique=True)
    op.create_index("idx_identity_sessions_user", "identity_sessions", ["user_id"])
    op.create_table(
        "identity_audit",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.String(), nullable=True),
        sa.Column("actor_display_name", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target_user_id", sa.String(), nullable=True),
        sa.Column("target_display_name", sa.String(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
    )
    op.create_index("idx_identity_audit_created", "identity_audit", ["created_at"])
    op.create_table(
        "identity_notification_reads",
        sa.Column("user_id", sa.String(), sa.ForeignKey("identity_users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("notification_id", sa.String(), sa.ForeignKey("notifications.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("read_at", sa.String(), nullable=False),
    )
    op.create_index("idx_identity_notification_reads_user", "identity_notification_reads", ["user_id"])
    op.create_index("idx_identity_notification_reads_notification", "identity_notification_reads", ["notification_id"])


def downgrade() -> None:
    op.drop_index("idx_identity_notification_reads_notification", table_name="identity_notification_reads")
    op.drop_index("idx_identity_notification_reads_user", table_name="identity_notification_reads")
    op.drop_table("identity_notification_reads")
    op.drop_index("idx_identity_audit_created", table_name="identity_audit")
    op.drop_table("identity_audit")
    op.drop_index("idx_identity_sessions_user", table_name="identity_sessions")
    op.drop_index("uq_identity_sessions_token_hash", table_name="identity_sessions")
    op.drop_table("identity_sessions")
    op.drop_index("idx_identity_scopes_user", table_name="identity_scopes")
    op.drop_table("identity_scopes")
    op.drop_index("idx_identity_users_role_active", table_name="identity_users")
    op.drop_index("uq_identity_users_username_normalized", table_name="identity_users")
    op.drop_table("identity_users")
    op.drop_index("idx_batches_owner", table_name="batches")
    op.drop_column("batches", "owner_user_id")
