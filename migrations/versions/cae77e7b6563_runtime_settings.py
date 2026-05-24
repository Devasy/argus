"""runtime settings

Revision ID: cae77e7b6563
Revises: e2058abaa1ee
Create Date: 2026-05-24 16:09:44

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'cae77e7b6563'
down_revision: Union[str, Sequence[str], None] = 'e2058abaa1ee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('runtime_settings',
        sa.Column('key', sa.Text(), nullable=False),
        sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('key'),
        schema='argus'
    )


def downgrade() -> None:
    op.drop_table('runtime_settings', schema='argus')
