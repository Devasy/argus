"""reviewer agents

Revision ID: 91a60ba7be26
Revises: e6e468d5bd81
Create Date: 2026-05-23 14:38:45

"""
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '91a60ba7be26'
down_revision: Union[str, Sequence[str], None] = 'e6e468d5bd81'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DESIGN_GUIDELINES = """You are a design/architecture reviewer looking at a whole
merge request. Comment on cross-cutting concerns: schema changes, API
contract changes, layering violations, new dependencies between modules,
data-flow and state-management changes, backward-compat breaks,
migration safety, cross-service contract changes, and error-handling
strategy shifts.
Use graph_diff_impact/graph_impact (when available) to check the blast radius
of changed functions before flagging a cross-cutting concern, and
list_module_skills/read_module_skill (when available) to check whether a
change violates a documented module workflow or invariant.
Max 6 findings; severity reflects long-term cost. Same evidence rules as any
finding: quote real lines you read via tools."""


def upgrade() -> None:
    op.create_table('reviewer_agents',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('current_version_id', sa.UUID(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
        schema='argus'
    )
    op.create_table('reviewer_agent_versions',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('agent_id', sa.UUID(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('guidelines', sa.Text(), nullable=False),
        sa.Column('model', sa.Text(), nullable=True),
        sa.Column('max_rounds', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('tool_allowlist', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_by', sa.Text(), nullable=True),
        sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('agent_id', 'version'),
        sa.ForeignKeyConstraint(['agent_id'], ['argus.reviewer_agents.id']),
        schema='argus'
    )
    op.create_foreign_key('fk_reviewer_agents_current_version',
                          'reviewer_agents', 'reviewer_agent_versions',
                          ['current_version_id'], ['id'],
                          source_schema='argus', referent_schema='argus')

    conn = op.get_bind()
    agent_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    conn.execute(sa.text(
        "INSERT INTO argus.reviewer_agents (id, name, description, enabled) "
        "VALUES (:id, 'design', 'Whole-MR architecture and design review', true)"
    ), {"id": agent_id})
    conn.execute(sa.text(
        "INSERT INTO argus.reviewer_agent_versions "
        "(id, agent_id, version, guidelines, max_rounds) "
        "VALUES (:id, :agent_id, 1, :guidelines, 10)"
    ), {"id": version_id, "agent_id": agent_id, "guidelines": DESIGN_GUIDELINES})
    conn.execute(sa.text(
        "UPDATE argus.reviewer_agents SET current_version_id = :vid WHERE id = :aid"
    ), {"vid": version_id, "aid": agent_id})


def downgrade() -> None:
    op.drop_constraint('fk_reviewer_agents_current_version', 'reviewer_agents',
                       schema='argus', type_='foreignkey')
    op.drop_table('reviewer_agent_versions', schema='argus')
    op.drop_table('reviewer_agents', schema='argus')
