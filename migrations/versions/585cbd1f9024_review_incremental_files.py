"""review incremental_files

Revision ID: 585cbd1f9024
Revises: c4e83a4df8c5
Create Date: 2026-07-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '585cbd1f9024'
down_revision: Union[str, Sequence[str], None] = 'c4e83a4df8c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reviews',
        sa.Column('incremental_files', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema='argus')


def downgrade() -> None:
    op.drop_column('reviews', 'incremental_files', schema='argus')
