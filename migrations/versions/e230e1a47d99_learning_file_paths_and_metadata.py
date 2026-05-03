"""learning file_paths and metadata

Revision ID: e230e1a47d99
Revises: f5a6b7c8d9e0
Create Date: 2026-07-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e230e1a47d99'
down_revision: Union[str, Sequence[str], None] = 'f5a6b7c8d9e0'
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
