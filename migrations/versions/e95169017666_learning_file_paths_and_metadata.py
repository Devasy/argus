"""learning file_paths and metadata

Revision ID: e95169017666
Revises: ed742e4d572a
Create Date: 2026-05-17 17:14:52

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e95169017666'
down_revision: Union[str, Sequence[str], None] = 'ed742e4d572a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('learnings',
        sa.Column('file_paths', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema='argus')
    op.add_column('learnings',
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema='argus')


def downgrade() -> None:
    op.drop_column('learnings', 'metadata', schema='argus')
    op.drop_column('learnings', 'file_paths', schema='argus')
