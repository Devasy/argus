"""repo sister links + reviews.sister_context

Per-repo opt-in read access to related repos (reference only) during reviews, and a record on each
review of which sister commits it could read.

Revision ID: e430a8e71ec0
Revises: a1f0c2d3e4b5
Create Date: 2026-09-24

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'e430a8e71ec0'
down_revision: Union[str, Sequence[str], None] = 'a1f0c2d3e4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "repo_sister_links",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sister_repo_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("match_source_branch", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=True,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["repo_id"], ["argus.repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sister_repo_id"], ["argus.repositories.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("repo_id", "sister_repo_id"),
        sa.CheckConstraint("repo_id <> sister_repo_id", name="sister_not_self_check"),
        schema="argus")
    op.create_index("ix_repo_sister_links_repo", "repo_sister_links", ["repo_id"],
                    schema="argus")
    op.add_column("reviews", sa.Column("sister_context", postgresql.JSONB(), nullable=True),
                  schema="argus")


def downgrade() -> None:
    op.drop_column("reviews", "sister_context", schema="argus")
    op.drop_index("ix_repo_sister_links_repo", table_name="repo_sister_links", schema="argus")
    op.drop_table("repo_sister_links", schema="argus")
