"""note disposition replied_unclassified

'rejected_with_rationale' was the fallback for any human reply the keyword
classifier did not recognise, so "we could not read this" was recorded as a
verdict. On MR 12, 9 of 10 "rejections" were the bot being right. This adds an
honest label for the unreadable case, which acceptance.py excludes from the
rate rather than counting against the bot.

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-09-08

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'f3a4b5c6d7e8'
down_revision: Union[str, Sequence[str], None] = 'e2f3a4b5c6d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = ("open", "accepted", "accepted_manually", "answered",
        "rejected_with_rationale", "dismissed_ambiguous", "resolved_no_answer")
_NEW = _OLD + ("replied_unclassified",)


def _values(vals: Sequence[str]) -> str:
    return ",".join(f"'{v}'" for v in vals)


def upgrade() -> None:
    op.drop_constraint("note_disposition_check", "notes", schema="argus")
    op.create_check_constraint(
        "note_disposition_check", "notes",
        f"disposition IN ({_values(_NEW)})", schema="argus")


def downgrade() -> None:
    op.execute("UPDATE argus.notes SET disposition = 'rejected_with_rationale' "
               "WHERE disposition = 'replied_unclassified'")
    op.drop_constraint("note_disposition_check", "notes", schema="argus")
    op.create_check_constraint(
        "note_disposition_check", "notes",
        f"disposition IN ({_values(_OLD)})", schema="argus")
