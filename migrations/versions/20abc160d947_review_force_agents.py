"""review force_agents column

Revision ID: 20abc160d947
Revises: 91a60ba7be26
Create Date: 2026-05-23 14:39:10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '20abc160d947'
down_revision: Union[str, Sequence[str], None] = '91a60ba7be26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reviews',
        sa.Column('force_agents', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema='argus')


def downgrade() -> None:
    op.drop_column('reviews', 'force_agents', schema='argus')
