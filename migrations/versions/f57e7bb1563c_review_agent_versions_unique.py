"""unique constraint on review reviewer agent versions join table

Revision ID: f57e7bb1563c
Revises: 851c26a89b75
Create Date: 2026-05-23 14:40:02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f57e7bb1563c'
down_revision: Union[str, Sequence[str], None] = '851c26a89b75'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        'review_reviewer_agent_versions_review_id_agent_version_id_key',
        'review_reviewer_agent_versions',
        ['review_id', 'agent_version_id'],
        schema='argus'
    )


def downgrade() -> None:
    op.drop_constraint(
        'review_reviewer_agent_versions_review_id_agent_version_id_key',
        'review_reviewer_agent_versions',
        schema='argus',
        type_='unique'
    )
