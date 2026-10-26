"""Initial DMR tables

Revision ID: 20260921_0001
Revises:
Create Date: 2026-09-21
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260921_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("records", sa.Column("id", sa.String(), primary_key=True), sa.Column("business_code", sa.String(), nullable=False), sa.Column("name", sa.String(), nullable=False), sa.Column("registrant", sa.String(), nullable=False), sa.Column("type", sa.String(), nullable=False), sa.Column("product", sa.String(), nullable=False), sa.Column("module", sa.String(), nullable=False), sa.Column("version", sa.String(), nullable=False), sa.Column("previous", sa.String()), sa.Column("file_name", sa.String()), sa.Column("date", sa.String()), sa.Column("controlled", sa.String()), sa.Column("source", sa.String()), sa.Column("owner", sa.String()), sa.Column("size", sa.String()), sa.Column("source_type", sa.String()), sa.Column("linked_bom", sa.String()), sa.Column("metadata", sa.Text()), sa.Column("created_at", sa.String()), sa.Column("updated_at", sa.String()))
    op.create_index("idx_records_identity", "records", ["registrant", "product", "module", "business_code"])
    op.create_table("files", sa.Column("id", sa.String(), primary_key=True), sa.Column("record_id", sa.String(), sa.ForeignKey("records.id"), nullable=False), sa.Column("name", sa.String(), nullable=False), sa.Column("version", sa.String(), nullable=False), sa.Column("status", sa.String(), nullable=False), sa.Column("date", sa.String()), sa.Column("source", sa.String()), sa.Column("size", sa.String()), sa.Column("type", sa.String()), sa.Column("storage", sa.String()), sa.Column("sha256", sa.String(), nullable=False), sa.Column("blob_path", sa.String(), nullable=False), sa.Column("metadata", sa.Text()), sa.Column("linked_bom", sa.String()), sa.Column("link_status", sa.String()), sa.Column("created_at", sa.String()))
    op.create_index("idx_files_record", "files", ["record_id", "status"])
    op.create_table("batches", sa.Column("id", sa.String(), primary_key=True), sa.Column("status", sa.String(), nullable=False), sa.Column("created_at", sa.String(), nullable=False), sa.Column("updated_at", sa.String(), nullable=False), sa.Column("summary", sa.Text()))
    op.create_table("batch_items", sa.Column("id", sa.String(), primary_key=True), sa.Column("batch_id", sa.String(), sa.ForeignKey("batches.id"), nullable=False), sa.Column("file_name", sa.String(), nullable=False), sa.Column("staging_file_id", sa.String()), sa.Column("record_id", sa.String()), sa.Column("context_record_id", sa.String()), sa.Column("code", sa.String()), sa.Column("registrant", sa.String()), sa.Column("type", sa.String()), sa.Column("product", sa.String()), sa.Column("module", sa.String()), sa.Column("version", sa.String()), sa.Column("linked_bom", sa.String()), sa.Column("notes", sa.Text()), sa.Column("metadata", sa.Text()), sa.Column("current_version", sa.String()), sa.Column("detected_version", sa.String()), sa.Column("status", sa.String(), nullable=False), sa.Column("recommendation", sa.Text()), sa.Column("sha256", sa.String(), nullable=False), sa.Column("size", sa.Integer(), nullable=False), sa.Column("result_message", sa.Text()), sa.Column("created_at", sa.String()), sa.Column("updated_at", sa.String()))
    op.create_table("exceptions", sa.Column("id", sa.String(), primary_key=True), sa.Column("title", sa.String(), nullable=False), sa.Column("detail", sa.Text(), nullable=False), sa.Column("type", sa.String(), nullable=False), sa.Column("record_id", sa.String()), sa.Column("item_id", sa.String()), sa.Column("batch_id", sa.String()), sa.Column("record_no", sa.String()), sa.Column("recommendation", sa.Text()), sa.Column("action", sa.Text()), sa.Column("status", sa.String(), nullable=False), sa.Column("created_at", sa.String()), sa.Column("resolved_at", sa.String()))
    op.create_table("notifications", sa.Column("id", sa.String(), primary_key=True), sa.Column("time", sa.String()), sa.Column("title", sa.String()), sa.Column("detail", sa.Text()), sa.Column("audience", sa.Text()), sa.Column("product", sa.String()), sa.Column("read", sa.Integer(), nullable=False, server_default="0"), sa.Column("created_at", sa.String()))
    op.create_table("logs", sa.Column("id", sa.String(), primary_key=True), sa.Column("time", sa.String()), sa.Column("user", sa.String()), sa.Column("action", sa.String()), sa.Column("object", sa.String()), sa.Column("change", sa.Text()), sa.Column("source", sa.String()), sa.Column("result", sa.String()), sa.Column("created_at", sa.String()))
    op.create_table("settings", sa.Column("key", sa.String(), primary_key=True), sa.Column("value", sa.Text()))


def downgrade() -> None:
    for table in ["settings", "logs", "notifications", "exceptions", "batch_items", "batches", "files", "records"]:
        op.drop_table(table)
