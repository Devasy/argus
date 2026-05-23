"""review publish flag

Lets a review run the full pipeline and record every finding without posting
anything to GitLab, so a fixed set of MRs can be re-reviewed repeatedly (and
across models) as a golden set. Deliberately a separate flag rather than a
Review.mode value: mode says what KIND of review this is (full/qa_scenarios)
and the two are orthogonal.

Revision ID: a39b81036d87
Revises: dd3430a80ecf
Create Date: 2026-05-23 14:43:42

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a39b81036d87'
down_revision: Union[str, Sequence[str], None] = 'dd3430a80ecf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reviews',
                  sa.Column('publish', sa.Boolean(), nullable=False,
                            server_default=sa.true()),
                  schema='argus')


def downgrade() -> None:
    op.drop_column('reviews', 'publish', schema='argus')
