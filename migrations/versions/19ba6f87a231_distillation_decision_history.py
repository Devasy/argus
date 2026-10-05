"""Preserve per-run distillation decisions separately from the mutable ledger."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "19ba6f87a231"
down_revision = "b4f8d6f470df"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("distillation_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("distillation_run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("argus.distillation_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("discussion_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("argus.discussions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("decision", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("distillation_run_id", "discussion_id"), schema="argus")
    op.create_index("ix_distillation_decisions_distillation_run_id", "distillation_decisions",
                    ["distillation_run_id"], schema="argus")
    # Earlier overwritten runs cannot be reconstructed. Preserve only the
    # surviving decisions, without inventing historical conversation content.
    op.execute("""
        INSERT INTO argus.distillation_decisions
            (distillation_run_id, discussion_id, content_hash, decision, created_at)
        SELECT distillation_run_id, discussion_id, content_hash,
            jsonb_build_object('discussion_id', discussion_id, 'thread_type', thread_type,
                'status', status, 'reply_verdict', reply_verdict,
                'verdict_reason', verdict_reason, 'learning_ids', COALESCE(learning_ids, '[]'::jsonb),
                'decision_reason', decision_reason), updated_at
        FROM argus.distill_threads WHERE distillation_run_id IS NOT NULL
    """)


def downgrade():
    op.drop_table("distillation_decisions", schema="argus")
