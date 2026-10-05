"""add repositories.learnings_cooldown_hours

Revision ID: d4a9e0b1f6c7
Revises: b7e1f4c9a203
Create Date: 2026-09-17 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4a9e0b1f6c7'
down_revision: Union[str, Sequence[str], None] = 'b7e1f4c9a203'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'repositories',
        sa.Column('learnings_cooldown_hours', sa.Integer(), nullable=False,
                  server_default='72'),
        schema='argus')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('repositories', 'learnings_cooldown_hours', schema='argus')
