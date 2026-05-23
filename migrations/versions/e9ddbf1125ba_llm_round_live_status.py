"""llm_round live status columns

Revision ID: e9ddbf1125ba
Revises: 5c0d2e66212f
Create Date: 2026-05-23 14:41:15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e9ddbf1125ba'
down_revision: Union[str, Sequence[str], None] = '5c0d2e66212f'
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
