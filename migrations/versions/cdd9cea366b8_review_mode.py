"""review mode column

Revision ID: cdd9cea366b8
Revises: e9ddbf1125ba
Create Date: 2026-05-23 14:41:39

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'cdd9cea366b8'
down_revision: Union[str, Sequence[str], None] = 'e9ddbf1125ba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reviews',
        sa.Column('mode', sa.Text(), nullable=False, server_default='full'),
        schema='argus')


def downgrade() -> None:
    op.drop_column('reviews', 'mode', schema='argus')
