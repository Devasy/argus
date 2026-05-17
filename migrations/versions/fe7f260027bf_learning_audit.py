"""learning audit

Revision ID: fe7f260027bf
Revises: 284f7a53dde7
Create Date: 2026-05-17 17:16:48

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB  # noqa: F401

revision: str = 'fe7f260027bf'
down_revision: Union[str, Sequence[str], None] = '284f7a53dde7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "argus"


def upgrade() -> None:
    op.create_table(
        "audit_runs",
        sa.Column("id", sa.Uuid(), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),
        sa.Column("commit_sha", sa.Text(), nullable=True),
        sa.Column("clusters_examined", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verdicts_written", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["repo_id"], [f"{SCHEMA}.repositories.id"]),
        sa.CheckConstraint("status IN ('running','done','failed')",
                          name="audit_run_status_check"),
        schema=SCHEMA,
    )

    op.create_table(
        "audit_verdicts",
        sa.Column("id", sa.Uuid(), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("audit_run_id", sa.Uuid(), nullable=False),
        sa.Column("learning_id", sa.Uuid(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("citations", JSONB(), nullable=True),
        sa.Column("related_learning_id", sa.Uuid(), nullable=True),
        sa.Column("proposed_action", sa.Text(), nullable=False, server_default="none"),
        sa.Column("state", sa.Text(), nullable=False, server_default="proposed"),
        sa.Column("applied_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["audit_run_id"], [f"{SCHEMA}.audit_runs.id"]),
        sa.ForeignKeyConstraint(["learning_id"], [f"{SCHEMA}.learnings.id"]),
        sa.ForeignKeyConstraint(["related_learning_id"], [f"{SCHEMA}.learnings.id"]),
        sa.CheckConstraint(
            "verdict IN ('corroborated','stale','contradicted','unfalsifiable',"
            "'duplicate_of','conflicts_with','ungrounded')",
            name="audit_verdict_check"),
        sa.CheckConstraint(
            "proposed_action IN ('none','archive','merge','flag_for_rewrite',"
            "'escalate_to_human')", name="audit_action_check"),
        sa.CheckConstraint("state IN ('proposed','approved','rejected','applied')",
                          name="audit_state_check"),
        schema=SCHEMA,
    )

    op.add_column("learnings",
                  sa.Column("groundedness", sa.Float(), nullable=True),
                  schema=SCHEMA)
    op.add_column("learnings",
                  sa.Column("last_audited_at", sa.TIMESTAMP(timezone=True), nullable=True),
                  schema=SCHEMA)
    op.add_column("learnings",
                  sa.Column("audited_at_sha", sa.Text(), nullable=True),
                  schema=SCHEMA)

    op.add_column("llm_rounds",
                  sa.Column("audit_run_id", sa.Uuid(), nullable=True),
                  schema=SCHEMA)
    op.create_foreign_key("llm_rounds_audit_run_id_fkey", "llm_rounds",
                          "audit_runs", ["audit_run_id"], ["id"],
                          source_schema=SCHEMA, referent_schema=SCHEMA)
    op.drop_constraint("llm_rounds_exactly_one_parent_check", "llm_rounds",
                       schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "llm_rounds_exactly_one_parent_check", "llm_rounds",
        "(review_id IS NOT NULL)::int + (distillation_run_id IS NOT NULL)::int"
        " + (audit_run_id IS NOT NULL)::int = 1",
        schema=SCHEMA)

    op.add_column("tool_calls",
                  sa.Column("audit_run_id", sa.Uuid(), nullable=True),
                  schema=SCHEMA)
    op.create_foreign_key("tool_calls_audit_run_id_fkey", "tool_calls",
                          "audit_runs", ["audit_run_id"], ["id"],
                          source_schema=SCHEMA, referent_schema=SCHEMA)
    op.drop_constraint("tool_calls_exactly_one_parent_check", "tool_calls",
                       schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "tool_calls_exactly_one_parent_check", "tool_calls",
        "(review_id IS NOT NULL)::int + (distillation_run_id IS NOT NULL)::int"
        " + (audit_run_id IS NOT NULL)::int = 1",
        schema=SCHEMA)

    op.create_index("ix_audit_verdicts_state", "audit_verdicts", ["state"],
                    schema=SCHEMA)
    op.create_index("ix_audit_verdicts_learning_id", "audit_verdicts",
                    ["learning_id"], schema=SCHEMA)
    op.create_index("ix_learnings_last_audited_at", "learnings",
                    ["last_audited_at"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_learnings_last_audited_at", table_name="learnings",
                  schema=SCHEMA)
    op.drop_index("ix_audit_verdicts_learning_id", table_name="audit_verdicts",
                  schema=SCHEMA)
    op.drop_index("ix_audit_verdicts_state", table_name="audit_verdicts",
                  schema=SCHEMA)

    op.drop_constraint("tool_calls_exactly_one_parent_check", "tool_calls",
                       schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "tool_calls_exactly_one_parent_check", "tool_calls",
        "(review_id IS NOT NULL) != (distillation_run_id IS NOT NULL)",
        schema=SCHEMA)
    op.drop_constraint("tool_calls_audit_run_id_fkey", "tool_calls",
                       schema=SCHEMA, type_="foreignkey")
    op.drop_column("tool_calls", "audit_run_id", schema=SCHEMA)

    op.drop_constraint("llm_rounds_exactly_one_parent_check", "llm_rounds",
                       schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "llm_rounds_exactly_one_parent_check", "llm_rounds",
        "(review_id IS NOT NULL) != (distillation_run_id IS NOT NULL)",
        schema=SCHEMA)
    op.drop_constraint("llm_rounds_audit_run_id_fkey", "llm_rounds",
                       schema=SCHEMA, type_="foreignkey")
    op.drop_column("llm_rounds", "audit_run_id", schema=SCHEMA)

    op.drop_column("learnings", "audited_at_sha", schema=SCHEMA)
    op.drop_column("learnings", "last_audited_at", schema=SCHEMA)
    op.drop_column("learnings", "groundedness", schema=SCHEMA)

    op.drop_table("audit_verdicts", schema=SCHEMA)
    op.drop_table("audit_runs", schema=SCHEMA)
