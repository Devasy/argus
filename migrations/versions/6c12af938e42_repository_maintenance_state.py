"""Keep audit and thread-sweep state separate from ingestion's poll cursor."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "6c12af938e42"
down_revision = "19ba6f87a231"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("repository_maintenance_state",
        sa.Column("repo_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("argus.repositories.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("audit_cursor", postgresql.JSONB(), nullable=True),
        sa.Column("thread_sweep_cursor", postgresql.JSONB(), nullable=True),
        schema="argus")
    op.create_index("ix_learnings_missing_embedding", "learnings", ["id"],
        schema="argus", postgresql_where=sa.text("status = 'active' AND embedding IS NULL"))
    op.create_index("ix_jobs_embedding_learning", "jobs",
        [sa.text("(payload ->> 'learning_id')"), "created_at"], schema="argus",
        postgresql_where=sa.text("kind = 'embed_learning'"))


def downgrade():
    op.drop_index("ix_jobs_embedding_learning", table_name="jobs", schema="argus")
    op.drop_index("ix_learnings_missing_embedding", table_name="learnings", schema="argus")
    op.drop_table("repository_maintenance_state", schema="argus")
