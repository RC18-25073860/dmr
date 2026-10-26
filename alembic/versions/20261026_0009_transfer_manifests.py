"""Keep emailed DMR transfer forms separate from controlled DMR file versions."""
from alembic import op
import sqlalchemy as sa

revision = '20261026_0009'
down_revision = '20260924_0008'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'transfer_manifests',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('client_id', sa.String(), nullable=False),
        sa.Column('mailbox', sa.String(), nullable=False),
        sa.Column('message_id', sa.String(), nullable=False),
        sa.Column('attachment_id', sa.String(), nullable=False),
        sa.Column('payload_fingerprint', sa.String(), nullable=False),
        sa.Column('filename', sa.String(), nullable=False),
        sa.Column('file_date', sa.String(), nullable=False),
        sa.Column('sha256', sa.String(), nullable=False),
        sa.Column('blob_path', sa.String(), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('target_path', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('last_error', sa.String(), nullable=False),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.Column('updated_at', sa.String(), nullable=False),
        sa.UniqueConstraint('mailbox', 'message_id', 'attachment_id', name='uq_transfer_manifest_mail_source'),
    )
    op.create_index('idx_transfer_manifests_status', 'transfer_manifests', ['status', 'updated_at'])


def downgrade():
    op.drop_index('idx_transfer_manifests_status', table_name='transfer_manifests')
    op.drop_table('transfer_manifests')
