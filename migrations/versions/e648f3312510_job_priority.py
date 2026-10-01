"""job queue priority

A bulk enqueue of one kind starved the other under plain FIFO: 194 distill_mr
jobs queued ahead of 14 waiting review jobs (2026-09) meant only distills got
claimed until all 194 drained, since claim_next ordered purely by
Job.created_at. Adds a priority column so review always claims ahead of
distill_mr, and both ahead of anything lower (benchmark dry-run reviews;
audit doesn't use this queue). Existing rows default to 100 (below every
named tier), so they drain after new work at a named priority, not before.

Revision ID: e648f3312510
Revises: d4a9e0b1f6c7
Create Date: 2026-09-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e648f3312510'
down_revision: Union[str, Sequence[str], None] = 'd4a9e0b1f6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        schema="argus")
    op.execute("UPDATE argus.jobs SET priority = 0 WHERE kind = 'review'")
    op.execute("UPDATE argus.jobs SET priority = 10 WHERE kind = 'distill_mr'")
    op.create_index("ix_jobs_priority_created_at", "jobs",
                    ["priority", "created_at"], schema="argus")


def downgrade() -> None:
    op.drop_index("ix_jobs_priority_created_at", table_name="jobs", schema="argus")
    op.drop_column("jobs", "priority", schema="argus")
