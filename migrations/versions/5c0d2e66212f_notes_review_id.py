"""notes review_id attribution column

Revision ID: 5c0d2e66212f
Revises: a7627d275ec0
Create Date: 2026-05-23 14:40:51

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '5c0d2e66212f'
down_revision: Union[str, Sequence[str], None] = 'a7627d275ec0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('notes',
        sa.Column('review_id', sa.Uuid(), nullable=True),
        schema='argus')
    op.create_foreign_key('notes_review_id_fkey', 'notes', 'reviews',
                          ['review_id'], ['id'],
                          source_schema='argus', referent_schema='argus')


def downgrade() -> None:
    op.drop_constraint('notes_review_id_fkey', 'notes', schema='argus')
    op.drop_column('notes', 'review_id', schema='argus')
