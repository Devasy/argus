"""review incremental_files

Revision ID: a7627d275ec0
Revises: f57e7bb1563c
Create Date: 2026-05-23 14:40:27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a7627d275ec0'
down_revision: Union[str, Sequence[str], None] = 'f57e7bb1563c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reviews',
        sa.Column('incremental_files', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema='argus')


def downgrade() -> None:
    op.drop_column('reviews', 'incremental_files', schema='argus')
