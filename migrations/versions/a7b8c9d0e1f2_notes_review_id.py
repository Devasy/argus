"""notes review_id attribution column

Revision ID: a7b8c9d0e1f2
Revises: 585cbd1f9024
Create Date: 2026-07-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = '585cbd1f9024'
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
