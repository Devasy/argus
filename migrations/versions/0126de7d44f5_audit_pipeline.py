"""audit_stages table, audit_runs progress columns, 'queued' status

Revision ID: 0126de7d44f5
Revises: b9f263e4cf86
Create Date: 2026-09-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0126de7d44f5"
down_revision: Union[str, Sequence[str], None] = "b9f263e4cf86"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_stages",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("audit_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),
        sa.Column("artifact", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", postgresql.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()")),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["audit_run_id"], ["argus.audit_runs.id"],
                                ondelete="CASCADE"),
        sa.UniqueConstraint("audit_run_id", "stage_name"),
        sa.CheckConstraint("status IN ('running','done','failed')",
                           name="audit_stage_status_check"),
        schema="argus")

    op.add_column("audit_runs",
                  sa.Column("trigger", sa.Text(), nullable=False, server_default="scheduled"),
                  schema="argus")
    op.add_column("audit_runs",
                  sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
                  schema="argus")
    op.add_column("audit_runs",
                  sa.Column("planned_count", sa.Integer(), nullable=False, server_default="0"),
                  schema="argus")
    op.add_column("audit_runs",
                  sa.Column("grounded_count", sa.Integer(), nullable=False, server_default="0"),
                  schema="argus")

    op.drop_constraint("audit_run_status_check", "audit_runs", schema="argus")
    op.create_check_constraint(
        "audit_run_status_check", "audit_runs",
        "status IN ('queued','running','done','failed')", schema="argus")


def downgrade() -> None:
    op.execute("UPDATE argus.audit_runs SET status='failed' WHERE status='queued'")
    op.drop_constraint("audit_run_status_check", "audit_runs", schema="argus")
    op.create_check_constraint(
        "audit_run_status_check", "audit_runs",
        "status IN ('running','done','failed')", schema="argus")

    op.drop_column("audit_runs", "grounded_count", schema="argus")
    op.drop_column("audit_runs", "planned_count", schema="argus")
    op.drop_column("audit_runs", "job_id", schema="argus")
    op.drop_column("audit_runs", "trigger", schema="argus")

    op.drop_table("audit_stages", schema="argus")
