"""review publish flag

Lets a review run the full pipeline and record every finding without posting
anything to GitLab, so a fixed set of MRs can be re-reviewed repeatedly (and
across models) as a golden set. Deliberately a separate flag rather than a
Review.mode value: mode says what KIND of review this is (full/qa_scenarios)
and the two are orthogonal.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-09-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e2f3a4b5c6d7'
down_revision: Union[str, Sequence[str], None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reviews',
                  sa.Column('publish', sa.Boolean(), nullable=False,
                            server_default=sa.true()),
                  schema='argus')


def downgrade() -> None:
    op.drop_column('reviews', 'publish', schema='argus')
