"""feedback kind 'followup' + note disposition 'accepted_by_followup'

Incremental re-reviews now check Argus's own earlier inline comments against the new code
(review/followup.py), record each check as feedback kind 'followup', and a verified fix becomes
disposition 'accepted_by_followup' (counted as accepted, and reported separately).

Revision ID: a1f0c2d3e4b5
Revises: e648f3312510
Create Date: 2026-09-24

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'a1f0c2d3e4b5'
down_revision: Union[str, Sequence[str], None] = 'e648f3312510'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "kind IN ('reply','applied','reaction','resolved','ui_answer')"
_NEW = "kind IN ('reply','applied','reaction','resolved','ui_answer','followup')"
_DISP = ("'open','accepted','accepted_manually','answered','rejected_with_rationale',"
         "'dismissed_ambiguous','resolved_no_answer','replied_unclassified'")


def upgrade() -> None:
    op.drop_constraint("feedback_kind_check", "feedback", schema="argus", type_="check")
    op.create_check_constraint("feedback_kind_check", "feedback", _NEW, schema="argus")
    op.drop_constraint("note_disposition_check", "notes", schema="argus", type_="check")
    op.create_check_constraint("note_disposition_check", "notes",
                               f"disposition IN ({_DISP},'accepted_by_followup')", schema="argus")


def downgrade() -> None:
    op.execute("UPDATE argus.notes SET disposition = 'open' WHERE disposition = 'accepted_by_followup'")
    op.drop_constraint("note_disposition_check", "notes", schema="argus", type_="check")
    op.create_check_constraint("note_disposition_check", "notes", f"disposition IN ({_DISP})",
                               schema="argus")
    op.execute("DELETE FROM argus.feedback WHERE kind = 'followup'")
    op.drop_constraint("feedback_kind_check", "feedback", schema="argus", type_="check")
    op.create_check_constraint("feedback_kind_check", "feedback", _OLD, schema="argus")
