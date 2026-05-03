"""review mode column

Revision ID: b3c4d5e6f7a8
Revises: c5d6e7f8a9b0
Create Date: 2026-08-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, Sequence[str], None] = 'c5d6e7f8a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reviews',
        sa.Column('mode', sa.Text(), nullable=False, server_default='full'),
        schema='argus')


def downgrade() -> None:
    op.drop_column('reviews', 'mode', schema='argus')
