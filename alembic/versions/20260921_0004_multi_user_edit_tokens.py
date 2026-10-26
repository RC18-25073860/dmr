"""Add local password lifecycle and edit revisions

Revision ID: 20260921_0004
Revises: 20260921_0003
Create Date: 2026-09-22
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260921_0004"
down_revision = "20260921_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("identity_users", sa.Column("must_change_password", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("identity_users", sa.Column("edit_revision", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("records", sa.Column("edit_revision", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("batch_items", sa.Column("edit_revision", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("batch_items", "edit_revision")
    op.drop_column("records", "edit_revision")
    op.drop_column("identity_users", "edit_revision")
    op.drop_column("identity_users", "must_change_password")
