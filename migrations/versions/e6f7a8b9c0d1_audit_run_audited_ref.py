"""record which ref an audit examined

A verdict of "this pattern does not exist in the codebase" is uncheckable
without knowing which codebase, at which revision. An audit that ran against
`master` declared a learning about data/rabbitmq/custom.conf nonexistent; the
file lives on develop-7.0.0.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-08-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e6f7a8b9c0d1'
down_revision: Union[str, Sequence[str], None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('audit_runs', sa.Column('audited_ref', sa.Text(), nullable=True),
                  schema='argus')


def downgrade() -> None:
    op.drop_column('audit_runs', 'audited_ref', schema='argus')
