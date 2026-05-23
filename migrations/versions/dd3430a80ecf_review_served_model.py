"""review served model

llm_config.model is effectively hardcoded: all 432 existing reviews record
'openai/qwen3.6-35b-a3b', including the weeks a different build was loaded
behind that same name. Langfuse's provided_model_name repeats the same
requested string, so no telemetry anywhere can attribute a review to the model
that actually produced it. This records what the endpoint reports serving.

Revision ID: dd3430a80ecf
Revises: f43205d5e615
Create Date: 2026-05-23 14:43:18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'dd3430a80ecf'
down_revision: Union[str, Sequence[str], None] = 'f43205d5e615'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reviews',
                  sa.Column('served_model', sa.Text(), nullable=True),
                  schema='argus')


def downgrade() -> None:
    op.drop_column('reviews', 'served_model', schema='argus')
