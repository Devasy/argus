"""llm_round live status columns

Revision ID: c5d6e7f8a9b0
Revises: b2c3d4e5f6a7
Create Date: 2026-08-01

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "argus"


def upgrade() -> None:
    op.add_column(
        "llm_rounds",
        sa.Column("status", sa.Text(), nullable=False, server_default="done"),
        schema=SCHEMA,
    )
    op.add_column(
        "llm_rounds",
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "llm_rounds_status_check",
        "llm_rounds",
        "status IN ('running', 'done', 'failed')",
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint("llm_rounds_status_check", "llm_rounds", schema=SCHEMA,
                       type_="check")
    op.drop_column("llm_rounds", "started_at", schema=SCHEMA)
    op.drop_column("llm_rounds", "status", schema=SCHEMA)
