"""unique constraint on review reviewer agent versions join table

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-07-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f5a6b7c8d9e0'
down_revision: Union[str, Sequence[str], None] = 'e4f5a6b7c8d9'
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
