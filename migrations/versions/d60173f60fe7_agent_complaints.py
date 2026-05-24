"""agent complaints

A channel for agents to report problems with their own tools, prompts, and
context. Until now that signal only existed inside `thinking` blocks, where it
took a human reading Langfuse traces to find it.

Revision ID: d60173f60fe7
Revises: 054660a998ff
Create Date: 2026-05-23 14:42:28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'd60173f60fe7'
down_revision: Union[str, Sequence[str], None] = '054660a998ff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agent_complaints',
        sa.Column('id', postgresql.UUID(as_uuid=True),
                  server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('review_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('distillation_run_id', postgresql.UUID(as_uuid=True),
                  nullable=True),
        sa.Column('audit_run_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('stage_name', sa.Text(), nullable=True),
        sa.Column('category', sa.Text(), nullable=False),
        sa.Column('target', sa.Text(), nullable=True),
        sa.Column('detail', sa.Text(), nullable=False),
        sa.Column('blocked', sa.Boolean(), nullable=False,
                  server_default='false'),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['review_id'], ['argus.reviews.id']),
        sa.ForeignKeyConstraint(['distillation_run_id'],
                                ['argus.distillation_runs.id']),
        sa.ForeignKeyConstraint(['audit_run_id'], ['argus.audit_runs.id']),
        sa.CheckConstraint(
            "category IN ('tool_broken','tool_missing','context_insufficient',"
            "'context_wrong','prompt_irrelevant','prompt_unclear',"
            "'instructions_conflict','task_impossible')",
            name='agent_complaint_category_check'),
        sa.CheckConstraint(
            "(review_id IS NOT NULL)::int + (distillation_run_id IS NOT NULL)::int"
            " + (audit_run_id IS NOT NULL)::int = 1",
            name='agent_complaints_exactly_one_parent_check'),
        schema='argus')
    # The dashboard question is "which tool is failing most", so index the
    # grouping key rather than the primary key alone.
    op.create_index('ix_agent_complaints_target', 'agent_complaints',
                    ['target'], schema='argus')
    op.create_index('ix_agent_complaints_created_at', 'agent_complaints',
                    ['created_at'], schema='argus')


def downgrade() -> None:
    op.drop_index('ix_agent_complaints_created_at', table_name='agent_complaints',
                  schema='argus')
    op.drop_index('ix_agent_complaints_target', table_name='agent_complaints',
                  schema='argus')
    op.drop_table('agent_complaints', schema='argus')
