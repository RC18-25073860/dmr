
"""Add local NAS account and sync handoff tables

Revision ID: 20260922_0005
Revises: 20260921_0004
Create Date: 2026-09-22
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260922_0005"
down_revision = "20260921_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nas_credentials",
        sa.Column("user_id", sa.String(), sa.ForeignKey("identity_users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("account", sa.String(), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("check_status", sa.String(), nullable=False, server_default="unverified"),
        sa.Column("check_error", sa.String(), nullable=False, server_default=""),
        sa.Column("checked_at", sa.String(), nullable=True),
        sa.Column("check_lease", sa.String(), nullable=True),
        sa.Column("check_until", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_index("idx_nas_credentials_check", "nas_credentials", ["check_status", "check_until"])
    op.create_table(
        "nas_directory_mappings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("record_id", sa.String(), sa.ForeignKey("records.id", ondelete="CASCADE"), nullable=False),
        sa.Column("nas_path", sa.String(), nullable=False),
        sa.Column("active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_index("uq_nas_directory_mappings_record", "nas_directory_mappings", ["record_id"], unique=True)
    op.create_table(
        "nas_sync_jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("file_id", sa.String(), sa.ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("record_id", sa.String(), sa.ForeignKey("records.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("owner_display_name", sa.String(), nullable=False),
        sa.Column("owner_nas_account_snapshot", sa.String(), nullable=False, server_default=""),
        sa.Column("credential_revision", sa.Integer(), nullable=True),
        sa.Column("target_path", sa.String(), nullable=False, server_default=""),
        sa.Column("target_name", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.String(), nullable=False),
        sa.Column("lease_token", sa.String(), nullable=True),
        sa.Column("lease_until", sa.String(), nullable=True),
        sa.Column("last_heartbeat_at", sa.String(), nullable=True),
        sa.Column("synced_at", sa.String(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_index("uq_nas_sync_jobs_file", "nas_sync_jobs", ["file_id"], unique=True)
    op.create_index("idx_nas_sync_jobs_queue", "nas_sync_jobs", ["status", "next_attempt_at"])
    op.create_table(
        "nas_agents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("last_seen_at", sa.String(), nullable=False),
    )
    op.create_table(
        "nas_read_tickets",
        sa.Column("ticket_hash", sa.String(), primary_key=True),
        sa.Column("file_id", sa.String(), sa.ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(), sa.ForeignKey("identity_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.Column("redeemed_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index("idx_nas_read_tickets_file", "nas_read_tickets", ["file_id"])


def downgrade() -> None:
    op.drop_index("idx_nas_read_tickets_file", table_name="nas_read_tickets")
    op.drop_table("nas_read_tickets")
    op.drop_table("nas_agents")
    op.drop_index("idx_nas_sync_jobs_queue", table_name="nas_sync_jobs")
    op.drop_index("uq_nas_sync_jobs_file", table_name="nas_sync_jobs")
    op.drop_table("nas_sync_jobs")
    op.drop_index("uq_nas_directory_mappings_record", table_name="nas_directory_mappings")
    op.drop_table("nas_directory_mappings")
    op.drop_index("idx_nas_credentials_check", table_name="nas_credentials")
    op.drop_table("nas_credentials")
