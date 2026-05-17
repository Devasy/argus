"""distillation runs and learnings traceability

Revision ID: 7141cc30eb55
Revises: b9b640476901
Create Date: 2026-05-17 17:15:35

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '7141cc30eb55'
down_revision: Union[str, Sequence[str], None] = 'b9b640476901'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'distillation_runs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('mr_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('argus.merge_requests.id'), nullable=False),
        sa.Column('trigger', sa.Text(), nullable=False, server_default='reconcile'),
        sa.Column('status', sa.Text(), nullable=False, server_default='queued'),
        sa.Column('note_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('prompt_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('completion_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('started_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('finished_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.CheckConstraint("status IN ('queued','running','done','failed')",
                           name='distillation_run_status_check'),
        schema='argus')

    op.add_column('learnings',
        sa.Column('mr_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('argus.merge_requests.id'), nullable=True),
        schema='argus')
    op.add_column('learnings',
        sa.Column('source_note_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('argus.notes.id'), nullable=True),
        schema='argus')
    op.add_column('learnings',
        sa.Column('learned_from_actor_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('argus.actors.id'), nullable=True),
        schema='argus')

    op.alter_column('tool_calls', 'review_id', nullable=True, schema='argus')
    op.add_column('tool_calls',
        sa.Column('distillation_run_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('argus.distillation_runs.id'), nullable=True),
        schema='argus')
    op.create_check_constraint(
        'tool_calls_exactly_one_parent_check', 'tool_calls',
        '(review_id IS NOT NULL) != (distillation_run_id IS NOT NULL)',
        schema='argus')

    op.alter_column('llm_rounds', 'review_id', nullable=True, schema='argus')
    op.add_column('llm_rounds',
        sa.Column('distillation_run_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('argus.distillation_runs.id'), nullable=True),
        schema='argus')
    op.create_check_constraint(
        'llm_rounds_exactly_one_parent_check', 'llm_rounds',
        '(review_id IS NOT NULL) != (distillation_run_id IS NOT NULL)',
        schema='argus')


def downgrade() -> None:
    op.drop_constraint('llm_rounds_exactly_one_parent_check', 'llm_rounds',
                       schema='argus')
    op.drop_column('llm_rounds', 'distillation_run_id', schema='argus')
    op.alter_column('llm_rounds', 'review_id', nullable=False, schema='argus')

    op.drop_constraint('tool_calls_exactly_one_parent_check', 'tool_calls',
                       schema='argus')
    op.drop_column('tool_calls', 'distillation_run_id', schema='argus')
    op.alter_column('tool_calls', 'review_id', nullable=False, schema='argus')

    op.drop_column('learnings', 'learned_from_actor_id', schema='argus')
    op.drop_column('learnings', 'source_note_id', schema='argus')
    op.drop_column('learnings', 'mr_id', schema='argus')

    op.drop_table('distillation_runs', schema='argus')
