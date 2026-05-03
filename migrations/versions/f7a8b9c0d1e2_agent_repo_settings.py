"""per-repo reviewer agent settings

Agents were global: every enabled specialist was offered to Scout on every
repository, including ones where it has nothing useful to contribute.

Opt-out by design -- no row means the agent runs, so this migration changes
no existing behaviour.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-08-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'f7a8b9c0d1e2'
down_revision: Union[str, Sequence[str], None] = 'e6f7a8b9c0d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'reviewer_agent_repo_settings',
        sa.Column('id', postgresql.UUID(as_uuid=True),
                  server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('agent_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('repo_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False,
                  server_default='true'),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['agent_id'], ['argus.reviewer_agents.id']),
        sa.ForeignKeyConstraint(['repo_id'], ['argus.repositories.id']),
        sa.UniqueConstraint('agent_id', 'repo_id'),
        schema='argus')
    op.create_index('ix_agent_repo_settings_repo', 'reviewer_agent_repo_settings',
                    ['repo_id'], schema='argus')


def downgrade() -> None:
    op.drop_index('ix_agent_repo_settings_repo',
                  table_name='reviewer_agent_repo_settings', schema='argus')
    op.drop_table('reviewer_agent_repo_settings', schema='argus')
