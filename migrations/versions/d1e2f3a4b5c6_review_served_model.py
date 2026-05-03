"""review served model

llm_config.model is effectively hardcoded: all 432 existing reviews record
'openai/qwen3.6-35b-a3b', including the weeks a different build was loaded
behind that same name. Langfuse's provided_model_name repeats the same
requested string, so no telemetry anywhere can attribute a review to the model
that actually produced it. This records what the endpoint reports serving.

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-09-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'c0d1e2f3a4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reviews',
                  sa.Column('served_model', sa.Text(), nullable=True),
                  schema='argus')


def downgrade() -> None:
    op.drop_column('reviews', 'served_model', schema='argus')
