"""distill_threads ledger (+ seed: mark every already-covered thread done)

Revision ID: b9f263e4cf86
Revises: e430a8e71ec0
Create Date: 2026-09-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b9f263e4cf86"
down_revision: Union[str, Sequence[str], None] = "e430a8e71ec0"
branch_labels = None
depends_on = None

# Same hash as distill_threads.content_hash(): md5 of sorted human note ids, byte-order ("C") sort.
_SEED = """
INSERT INTO argus.distill_threads
    (mr_id, discussion_id, thread_type, content_hash, status, attempts, seeded)
SELECT d.mr_id, d.id,
       CASE WHEN EXISTS (SELECT 1 FROM argus.notes b
                         WHERE b.discussion_id = d.id AND b.author_type = 'bot')
            THEN 'bot_thread' ELSE 'human_thread' END,
       md5(string_agg(n.id::text, ',' ORDER BY n.id::text COLLATE "C")),
       'done', 0, true
FROM argus.discussions d
JOIN argus.notes n ON n.discussion_id = d.id
     AND n.author_type = 'human' AND n.kind IN ('inline', 'summary')
GROUP BY d.id, d.mr_id
-- bot threads with a human reply were never really distilled: leave them for the catch-up
HAVING NOT EXISTS (SELECT 1 FROM argus.notes b
                   WHERE b.discussion_id = d.id AND b.author_type = 'bot'
                     AND b.note_created_at < max(n.note_created_at))
"""


def upgrade() -> None:
    op.create_table(
        "distill_threads",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("mr_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("discussion_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_type", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("distillation_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reply_verdict", sa.Text(), nullable=True),
        sa.Column("verdict_reason", sa.Text(), nullable=True),
        sa.Column("learning_ids", postgresql.JSONB(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("seeded", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()")),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["mr_id"], ["argus.merge_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["discussion_id"], ["argus.discussions.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["distillation_run_id"], ["argus.distillation_runs.id"],
                                ondelete="SET NULL"),
        sa.UniqueConstraint("discussion_id"),
        sa.CheckConstraint("thread_type IN ('bot_thread','human_thread')",
                           name="distill_thread_type_check"),
        sa.CheckConstraint("status IN ('queued','done','failed')",
                           name="distill_thread_status_check"),
        sa.CheckConstraint("reply_verdict IS NULL OR reply_verdict IN "
                           "('accepted','rejected','acknowledged','question','unclear')",
                           name="distill_thread_verdict_check"),
        schema="argus")
    op.create_index("ix_distill_threads_mr", "distill_threads", ["mr_id"], schema="argus")
    op.execute(_SEED)


def downgrade() -> None:
    op.drop_index("ix_distill_threads_mr", table_name="distill_threads", schema="argus")
    op.drop_table("distill_threads", schema="argus")
