"""distillation run langfuse trace id

Reviews and audit runs are traceable in Langfuse; distillation runs had no
Langfuse instrumentation at all.

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-09-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c0d1e2f3a4b5'
down_revision: Union[str, Sequence[str], None] = 'b9c0d1e2f3a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('distillation_runs',
                  sa.Column('langfuse_trace_id', sa.Text(), nullable=True),
                  schema='argus')


def downgrade() -> None:
    op.drop_column('distillation_runs', 'langfuse_trace_id', schema='argus')
