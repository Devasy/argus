"""repository auto_review_enabled column

Revision ID: a1b2c3d4e5f6
Revises: e230e1a47d99
Create Date: 2026-07-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'e230e1a47d99'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('repositories',
        sa.Column('auto_review_enabled', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        schema='argus')


def downgrade() -> None:
    op.drop_column('repositories', 'auto_review_enabled', schema='argus')
