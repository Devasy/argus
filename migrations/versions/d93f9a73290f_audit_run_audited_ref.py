"""record which ref an audit examined

A verdict of "this pattern does not exist in the codebase" is uncheckable
without knowing which codebase, at which revision. An audit that ran against
`master` declared a learning about data/rabbitmq/custom.conf nonexistent; the
file lives on develop-7.0.0.

Revision ID: d93f9a73290f
Revises: fe7f260027bf
Create Date: 2026-05-17 17:17:15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd93f9a73290f'
down_revision: Union[str, Sequence[str], None] = 'fe7f260027bf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('audit_runs', sa.Column('audited_ref', sa.Text(), nullable=True),
                  schema='argus')


def downgrade() -> None:
    op.drop_column('audit_runs', 'audited_ref', schema='argus')
