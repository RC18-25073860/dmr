"""Add NAS directory creation jobs

Revision ID: 20260923_0007
Revises: 20260922_0006
Create Date: 2026-09-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260923_0007"
down_revision = "20260922_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nas_directory_creation_jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("record_id", sa.String(), sa.ForeignKey("records.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_path", sa.String(), nullable=False),
        sa.Column("folder_name", sa.String(), nullable=False),
        sa.Column("nas_path", sa.String(), nullable=False),
        sa.Column("record_snapshot", sa.Text(), nullable=False),
        sa.Column("mapping_snapshot", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.String(), sa.ForeignKey("identity_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("requested_display_name", sa.String(), nullable=False),
        sa.Column("nas_account_snapshot", sa.String(), nullable=False),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("lease_token", sa.String(), nullable=True),
        sa.Column("lease_until", sa.String(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=False, server_default=""),
        sa.Column("completed_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_index("idx_nas_dir_jobs_queue", "nas_directory_creation_jobs", ["status", "created_at"])
    op.create_index("idx_nas_dir_jobs_record", "nas_directory_creation_jobs", ["record_id", "updated_at"])
    op.create_index("idx_nas_dir_jobs_path", "nas_directory_creation_jobs", ["nas_path", "status"])


def downgrade() -> None:
    op.drop_index("idx_nas_dir_jobs_path", table_name="nas_directory_creation_jobs")
    op.drop_index("idx_nas_dir_jobs_record", table_name="nas_directory_creation_jobs")
    op.drop_index("idx_nas_dir_jobs_queue", table_name="nas_directory_creation_jobs")
    op.drop_table("nas_directory_creation_jobs")
