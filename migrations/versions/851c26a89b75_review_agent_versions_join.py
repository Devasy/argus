"""review reviewer agent versions join table

Revision ID: 851c26a89b75
Revises: 20abc160d947
Create Date: 2026-05-23 14:39:34

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '851c26a89b75'
down_revision: Union[str, Sequence[str], None] = '20abc160d947'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('review_reviewer_agent_versions',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('review_id', sa.UUID(), nullable=False),
        sa.Column('agent_version_id', sa.UUID(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['review_id'], ['argus.reviews.id']),
        sa.ForeignKeyConstraint(['agent_version_id'], ['argus.reviewer_agent_versions.id']),
        schema='argus'
    )


def downgrade() -> None:
    op.drop_table('review_reviewer_agent_versions', schema='argus')
