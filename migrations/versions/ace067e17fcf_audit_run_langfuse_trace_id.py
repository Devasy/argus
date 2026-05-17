"""audit run langfuse trace id

Reviews and distillation runs are traceable; audit runs were not. An audit
that produces a surprising verdict -- or stalls at one cluster for eleven
minutes -- had no trace to open.

Revision ID: ace067e17fcf
Revises: 2af6a70c0f90
Create Date: 2026-05-17 17:18:05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'ace067e17fcf'
down_revision: Union[str, Sequence[str], None] = '2af6a70c0f90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('audit_runs',
                  sa.Column('langfuse_trace_id', sa.Text(), nullable=True),
                  schema='argus')


def downgrade() -> None:
    op.drop_column('audit_runs', 'langfuse_trace_id', schema='argus')
