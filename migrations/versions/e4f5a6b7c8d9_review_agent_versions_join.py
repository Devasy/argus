"""review reviewer agent versions join table

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-07-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e4f5a6b7c8d9'
down_revision: Union[str, Sequence[str], None] = 'd3e4f5a6b7c8'
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
