"""distillation_runs.id missing server default (bugfix)

The original distillation_runs migration created `id` as a bare UUID
primary key with no server_default, unlike every other table's id column
(gen_random_uuid()) and unlike the DistillationRun model's own
mapped_column(..., server_default=text("gen_random_uuid()")). Inserts
that rely on the DB to generate the id (as the ORM does) fail with
NotNullViolationError in any environment where the table was actually
created via this migration (not via Base.metadata.create_all, which
reads the correct default straight from the model and never hit this).

Revision ID: ff4a5ba5d6e9
Revises: 7141cc30eb55
Create Date: 2026-05-17 17:16:02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'ff4a5ba5d6e9'
down_revision: Union[str, Sequence[str], None] = '7141cc30eb55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('distillation_runs', 'id',
                    server_default=sa.text('gen_random_uuid()'),
                    schema='argus')


def downgrade() -> None:
    op.alter_column('distillation_runs', 'id',
                    server_default=None,
                    schema='argus')
