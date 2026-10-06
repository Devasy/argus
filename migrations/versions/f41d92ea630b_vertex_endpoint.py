"""Allow explicit Vertex AI model endpoints."""
from alembic import op

revision = "f41d92ea630b"
down_revision = "a7c93d51b204"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("endpoint_provider_check", "llm_endpoints", schema="argus", type_="check")
    op.create_check_constraint("endpoint_provider_check", "llm_endpoints",
        "provider IN ('anthropic','openai','gemini','ollama','claude_cli_proxy','groq','vertex_ai')", schema="argus")


def downgrade():
    # Retain endpoints, references, and credentials: the old application can use
    # the same Gemini-compatible transport with this explicit Vertex base URL.
    op.execute("UPDATE argus.llm_endpoints SET provider = 'gemini', model = regexp_replace(model, '^vertex_ai/', 'gemini/') WHERE provider = 'vertex_ai'")
    op.drop_constraint("endpoint_provider_check", "llm_endpoints", schema="argus", type_="check")
    op.create_check_constraint("endpoint_provider_check", "llm_endpoints",
        "provider IN ('anthropic','openai','gemini','ollama','claude_cli_proxy','groq')", schema="argus")
