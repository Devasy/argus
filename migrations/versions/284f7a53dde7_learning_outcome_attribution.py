"""learning outcome attribution

Revision ID: 284f7a53dde7
Revises: ff4a5ba5d6e9
Create Date: 2026-05-17 17:16:24

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB  # noqa: F401

revision: str = '284f7a53dde7'
down_revision: Union[str, Sequence[str], None] = 'ff4a5ba5d6e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "argus"


def upgrade() -> None:
    op.add_column("learnings",
                  sa.Column("harmful_count", sa.Integer(), nullable=False,
                            server_default="0"), schema=SCHEMA)
    op.add_column("learnings",
                  sa.Column("ignored_count", sa.Integer(), nullable=False,
                            server_default="0"), schema=SCHEMA)
    op.add_column("findings",
                  sa.Column("contributing_learning_ids",
                            JSONB(), nullable=True),
                  schema=SCHEMA)
    op.add_column("injection_events",
                  sa.Column("finding_id", sa.Uuid(), nullable=True),
                  schema=SCHEMA)
    op.create_foreign_key("injection_events_finding_id_fkey", "injection_events",
                          "findings", ["finding_id"], ["id"],
                          source_schema=SCHEMA, referent_schema=SCHEMA)
    op.drop_constraint("injection_outcome_check", "injection_events",
                       schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "injection_outcome_check", "injection_events",
        "outcome IN ('pending','hit','miss','inconclusive','harmful','ignored')",
        schema=SCHEMA)
    # Resolving an MR's injections needs "all events for this review" fast.
    op.create_index("ix_injection_events_review_id", "injection_events",
                    ["review_id"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_injection_events_review_id", table_name="injection_events",
                  schema=SCHEMA)
    op.drop_constraint("injection_outcome_check", "injection_events",
                       schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "injection_outcome_check", "injection_events",
        "outcome IN ('pending','hit','miss','inconclusive')", schema=SCHEMA)
    op.drop_constraint("injection_events_finding_id_fkey", "injection_events",
                       schema=SCHEMA, type_="foreignkey")
    op.drop_column("injection_events", "finding_id", schema=SCHEMA)
    op.drop_column("findings", "contributing_learning_ids", schema=SCHEMA)
    op.drop_column("learnings", "ignored_count", schema=SCHEMA)
    op.drop_column("learnings", "harmful_count", schema=SCHEMA)
