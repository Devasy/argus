"""learning outcome attribution

Revision ID: a1f2c3d4e5b6
Revises: a7b8c9d0e1f2
Create Date: 2026-07-31

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB  # noqa: F401

revision: str = "a1f2c3d4e5b6"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
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
