"""external_bot authors; reopen human threads the ledger seed closed unread

1. argus's own account was filed 'human' on 254 notes (Jun-Aug) whenever the
   poller started without resolving its username. Any account with a 'bot' note
   is ours, so all its notes become 'bot'.
2. Other review bots (GitLab project tokens) were 'human' and taught "human"
   learnings. Only recognized token-bot usernames become 'external_bot',
   which no human or bot query matches.
3. The distill_threads seed (b9f263e4cf86) marked every human-only thread done.
   1,820 of them never produced a learning; deleting those ledger rows lets the
   distill sweep read them.

Revision ID: b4f8d6f470df
Revises: 0126de7d44f5
Create Date: 2026-10-01

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'b4f8d6f470df'
down_revision: Union[str, Sequence[str], None] = '0126de7d44f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same token-bot format as argus.gitlab.normalizer.TOKEN_BOT_USERNAME.
_TOKEN_BOT = r"^(project|group)_[0-9]+_bot_[0-9a-zA-Z]+$"


def _check(values: str) -> None:
    op.execute("ALTER TABLE argus.notes DROP CONSTRAINT IF EXISTS note_author_type_check")
    op.execute(f"ALTER TABLE argus.notes ADD CONSTRAINT note_author_type_check "
               f"CHECK (author_type IN ({values}))")


def upgrade() -> None:
    _check("'bot','human','system','external_bot'")
    op.execute("""
        WITH ours AS (SELECT DISTINCT author_id FROM argus.notes
                      WHERE author_type = 'bot' AND author_id IS NOT NULL)
        UPDATE argus.notes n SET author_type = 'bot'
        FROM ours WHERE n.author_id = ours.author_id AND n.author_type = 'human'""")
    op.execute(f"""
        UPDATE argus.notes n SET author_type = 'external_bot'
        FROM argus.actors a
        WHERE a.id = n.author_id AND n.author_type = 'human' AND a.username ~* '{_TOKEN_BOT}'""")
    op.execute("""
        UPDATE argus.actors a SET is_bot = true
        WHERE EXISTS (SELECT 1 FROM argus.notes n WHERE n.author_id = a.id
                      AND n.author_type IN ('bot', 'external_bot'))""")
    op.execute("""
        DELETE FROM argus.distill_threads t
        WHERE t.seeded AND t.distillation_run_id IS NULL
          AND NOT EXISTS (SELECT 1 FROM argus.learnings l
                          JOIN argus.notes n ON n.id = l.source_note_id
                          WHERE n.discussion_id = t.discussion_id)""")


def downgrade() -> None:
    # Reopened ledger rows are not restored: the sweep has distilled them by now.
    op.execute("UPDATE argus.notes SET author_type = 'human' WHERE author_type = 'external_bot'")
    _check("'bot','human','system'")
