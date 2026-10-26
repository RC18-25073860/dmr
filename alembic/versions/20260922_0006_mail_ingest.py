"""Add mail ingest clients and receipts

Revision ID: 20260922_0006
Revises: 20260922_0005
Create Date: 2026-09-22
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260922_0006"
down_revision = "20260922_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mail_clients",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.String(), sa.ForeignKey("identity_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("mailbox", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.Column("revoked_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("created_by_user_id", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_index("uq_mail_clients_token_hash", "mail_clients", ["token_hash"], unique=True)
    op.create_index("idx_mail_clients_owner", "mail_clients", ["owner_user_id"])
    op.create_index("idx_mail_clients_mailbox", "mail_clients", ["mailbox"])
    op.create_table(
        "mail_receipts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("client_id", sa.String(), sa.ForeignKey("mail_clients.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("mailbox", sa.String(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=False),
        sa.Column("attachment_id", sa.String(), nullable=False),
        sa.Column("payload_fingerprint", sa.String(), nullable=False),
        sa.Column("batch_id", sa.String(), sa.ForeignKey("batches.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("batch_item_id", sa.String(), sa.ForeignKey("batch_items.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_index("uq_mail_receipts_message_attachment", "mail_receipts", ["mailbox", "message_id", "attachment_id"], unique=True)
    op.create_index("idx_mail_receipts_client", "mail_receipts", ["client_id", "created_at"])
    op.create_index("idx_mail_receipts_batch", "mail_receipts", ["batch_id", "batch_item_id"])


def downgrade() -> None:
    op.drop_index("idx_mail_receipts_batch", table_name="mail_receipts")
    op.drop_index("idx_mail_receipts_client", table_name="mail_receipts")
    op.drop_index("uq_mail_receipts_message_attachment", table_name="mail_receipts")
    op.drop_table("mail_receipts")
    op.drop_index("idx_mail_clients_mailbox", table_name="mail_clients")
    op.drop_index("idx_mail_clients_owner", table_name="mail_clients")
    op.drop_index("uq_mail_clients_token_hash", table_name="mail_clients")
    op.drop_table("mail_clients")
