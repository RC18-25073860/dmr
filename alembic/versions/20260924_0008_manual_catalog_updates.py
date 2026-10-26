"""Durable manual cloud-catalog reminders; no cloud writes."""
from alembic import op
import sqlalchemy as sa
revision = '20260924_0008'
down_revision = '20260923_0007'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('manual_catalog_updates',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('record_id', sa.String(), nullable=False),
        sa.Column('file_id', sa.String(), nullable=False, unique=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.Column('updated_at', sa.String(), nullable=False))
    op.create_index('idx_manual_catalog_record', 'manual_catalog_updates', ['record_id', 'status'])

def downgrade():
    op.drop_index('idx_manual_catalog_record', table_name='manual_catalog_updates')
    op.drop_table('manual_catalog_updates')
