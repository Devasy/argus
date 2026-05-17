"""distillation run langfuse trace id

Reviews and audit runs are traceable in Langfuse; distillation runs had no
Langfuse instrumentation at all.

Revision ID: c1c897cb7d36
Revises: ace067e17fcf
Create Date: 2026-05-17 17:18:31

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c1c897cb7d36'
down_revision: Union[str, Sequence[str], None] = 'ace067e17fcf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('distillation_runs',
                  sa.Column('langfuse_trace_id', sa.Text(), nullable=True),
                  schema='argus')


def downgrade() -> None:
    op.drop_column('distillation_runs', 'langfuse_trace_id', schema='argus')
