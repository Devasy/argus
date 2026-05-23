"""finding question type and outside_diff flag

Adds 'question' to the allowed finding types and an outside_diff flag.

A 'question' finding is not a defect claim -- it is an open question for the
MR author, raised when a change looks unrelated to the MR's stated intent.
Human reviewers ask these constantly and the bot had no way to express one,
so it either stayed silent or dressed a question up as a weak "suggestion".

outside_diff marks findings about code the MR did not touch (typically a
caller the change breaks). GitLab cannot anchor an inline comment outside the
diff, so these are reported in the summary; the column keeps them
distinguishable from findings that merely lost the inline budget.

Revision ID: 054660a998ff
Revises: cdd9cea366b8
Create Date: 2026-05-23 14:42:04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '054660a998ff'
down_revision: Union[str, Sequence[str], None] = 'cdd9cea366b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('finding_type_check', 'findings', schema='argus',
                       type_='check')
    op.create_check_constraint(
        'finding_type_check', 'findings',
        "type IN ('issue','suggestion','question')", schema='argus')
    op.add_column(
        'findings',
        sa.Column('outside_diff', sa.Boolean(), nullable=False,
                  server_default='false'),
        schema='argus')


def downgrade() -> None:
    op.drop_column('findings', 'outside_diff', schema='argus')
    # Existing 'question' rows would violate the narrowed constraint, so
    # reclassify them rather than failing the downgrade outright.
    op.execute("UPDATE argus.findings SET type = 'suggestion' "
               "WHERE type = 'question'")
    op.drop_constraint('finding_type_check', 'findings', schema='argus',
                       type_='check')
    op.create_check_constraint(
        'finding_type_check', 'findings',
        "type IN ('issue','suggestion')", schema='argus')
