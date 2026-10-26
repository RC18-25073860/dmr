"""Add project category fields

Revision ID: 20260921_0002
Revises: 20260921_0001
Create Date: 2026-09-21
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260921_0002"
down_revision = "20260921_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("records", sa.Column("project_category", sa.String(), nullable=False, server_default="待确认"))
    op.add_column("records", sa.Column("project_category_source", sa.String(), nullable=True, server_default="migration_default"))
    op.add_column("batch_items", sa.Column("project_category", sa.String(), nullable=True, server_default="待确认"))
    op.add_column("batch_items", sa.Column("project_category_source", sa.String(), nullable=True, server_default="migration_default"))


def downgrade() -> None:
    op.drop_column("batch_items", "project_category_source")
    op.drop_column("batch_items", "project_category")
    op.drop_column("records", "project_category_source")
    op.drop_column("records", "project_category")
