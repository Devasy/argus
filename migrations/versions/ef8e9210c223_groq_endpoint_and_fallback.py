"""groq endpoint provider + per-endpoint fallback

Adds 'groq' to the allowed llm_endpoints.provider values (fast hosted
inference, used for the Qwen3.8 weekend benchmark to get around how slow the
local llama.cpp box is), and a self-referential fallback_endpoint_id so an
endpoint can name a second llm_endpoints row to fail over to. build_chat_model
wires the resolved fallback into litellm's own `fallbacks=` mechanism, so a
Groq rate limit or outage fails a request over to the local endpoint instead
of failing the whole review.

No existing row is changed by this migration -- fallback_endpoint_id is
nullable and defaults to NULL, so behaviour is unchanged until a row opts in.

Revision ID: ef8e9210c223
Revises: cae77e7b6563
Create Date: 2026-09-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'ef8e9210c223'
down_revision: Union[str, Sequence[str], None] = 'cae77e7b6563'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('endpoint_provider_check', 'llm_endpoints',
                       schema='argus', type_='check')
    op.create_check_constraint(
        'endpoint_provider_check', 'llm_endpoints',
        "provider IN ('anthropic','openai','gemini','ollama',"
        "'claude_cli_proxy','groq')", schema='argus')
    op.add_column(
        'llm_endpoints',
        sa.Column('fallback_endpoint_id', postgresql.UUID(as_uuid=True),
                  nullable=True),
        schema='argus')
    op.create_foreign_key(
        'llm_endpoints_fallback_endpoint_id_fkey', 'llm_endpoints',
        'llm_endpoints', ['fallback_endpoint_id'], ['id'],
        source_schema='argus', referent_schema='argus')


def downgrade() -> None:
    op.drop_constraint('llm_endpoints_fallback_endpoint_id_fkey',
                       'llm_endpoints', schema='argus', type_='foreignkey')
    op.drop_column('llm_endpoints', 'fallback_endpoint_id', schema='argus')
    op.drop_constraint('endpoint_provider_check', 'llm_endpoints',
                       schema='argus', type_='check')
    op.create_check_constraint(
        'endpoint_provider_check', 'llm_endpoints',
        "provider IN ('anthropic','openai','gemini','ollama',"
        "'claude_cli_proxy')", schema='argus')
