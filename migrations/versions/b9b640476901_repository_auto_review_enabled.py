"""repository auto_review_enabled column

Revision ID: b9b640476901
Revises: e95169017666
Create Date: 2026-05-17 17:15:10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b9b640476901'
down_revision: Union[str, Sequence[str], None] = 'e95169017666'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('repositories',
        sa.Column('auto_review_enabled', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        schema='argus')


def downgrade() -> None:
    op.drop_column('repositories', 'auto_review_enabled', schema='argus')
