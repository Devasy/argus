"""audit verdict suggested_hint_text

Concrete replacement text for actions that rewrite a learning rather than
remove it (flag_for_rewrite, merge).

Before this, approving such a verdict did nothing: apply_verdict handled only
archive and merge, so "this learning is unfalsifiable, flag it for rewrite"
was recorded, approved by a human, and then silently dropped. Naming the
replacement is what turns approval into an action.

Revision ID: 2af6a70c0f90
Revises: d93f9a73290f
Create Date: 2026-05-17 17:17:39

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '2af6a70c0f90'
down_revision: Union[str, Sequence[str], None] = 'd93f9a73290f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('audit_verdicts',
                  sa.Column('suggested_hint_text', sa.Text(), nullable=True),
                  schema='argus')


def downgrade() -> None:
    op.drop_column('audit_verdicts', 'suggested_hint_text', schema='argus')
