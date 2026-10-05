"""add repositories.stale_mr_after_days

Revision ID: b7e1f4c9a203
Revises: ef8e9210c223
Create Date: 2026-09-17 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e1f4c9a203'
down_revision: Union[str, Sequence[str], None] = 'ef8e9210c223'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'repositories',
        sa.Column('stale_mr_after_days', sa.Integer(), nullable=False,
                  server_default='30'),
        schema='argus')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('repositories', 'stale_mr_after_days', schema='argus')
