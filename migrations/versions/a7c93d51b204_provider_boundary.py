"""Add provider identities and saved review publication artifacts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a7c93d51b204"
down_revision = "6c12af938e42"
branch_labels = None
depends_on = None


def upgrade():
    for table in ["learnings", "file_knowledge"]:
        op.add_column(table, sa.Column("embedding_fingerprint", sa.Text()), schema="argus")
    op.alter_column("repositories", "gitlab_project_id", nullable=True, schema="argus")
    op.add_column("repositories", sa.Column("provider_project_id", sa.Text()), schema="argus")
    op.execute("UPDATE argus.repositories SET provider_project_id = gitlab_project_id::text")
    op.alter_column("actors", "provider_user_id", nullable=True, schema="argus")
    op.add_column("actors", sa.Column("provider_user_key", sa.Text()), schema="argus")
    op.execute("UPDATE argus.actors SET provider_user_key = provider_user_id::text")
    op.create_unique_constraint("actors_provider_provider_user_key_key", "actors",
        ["provider", "provider_user_key"], schema="argus")
    op.add_column("repositories", sa.Column("default_llm_endpoint_id", postgresql.UUID(),
        sa.ForeignKey("argus.llm_endpoints.id")), schema="argus")
    op.add_column("repositories", sa.Column("embedding_config", postgresql.JSONB()), schema="argus")
    op.alter_column("mr_versions", "provider_version_id", nullable=True, schema="argus")
    op.add_column("mr_versions", sa.Column("snapshot_key", sa.Text()), schema="argus")
    op.create_unique_constraint("mr_versions_mr_id_snapshot_key_key", "mr_versions",
        ["mr_id", "snapshot_key"], schema="argus")
    op.add_column("notes", sa.Column("provider_note_kind", sa.Text(), nullable=False,
        server_default="note"), schema="argus")
    op.drop_constraint("notes_mr_id_provider_note_id_key", "notes", schema="argus", type_="unique")
    op.create_unique_constraint("notes_mr_id_provider_note_id_provider_note_kind_key", "notes",
        ["mr_id", "provider_note_id", "provider_note_kind"], schema="argus")
    for name, typ in [("publication_artifact", postgresql.JSONB()),
                      ("publication_result", postgresql.JSONB())]:
        op.add_column("reviews", sa.Column(name, typ), schema="argus")
    op.add_column("reviews", sa.Column("publication_status", sa.Text(), nullable=False,
        server_default="none"), schema="argus")


def downgrade():
    # Older application versions cannot represent these provider records.
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM argus.repositories WHERE provider <> 'gitlab')")).scalar():
        raise RuntimeError("Export/remove non-GitLab repositories before downgrading the provider schema")
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM argus.actors WHERE provider_user_id IS NULL)")).scalar():
        raise RuntimeError("Export/remove actors with opaque identities before downgrading the provider schema")
    op.drop_constraint("actors_provider_provider_user_key_key", "actors", schema="argus", type_="unique")
    op.drop_column("actors", "provider_user_key", schema="argus")
    op.alter_column("actors", "provider_user_id", nullable=False, schema="argus")
    for table in ["learnings", "file_knowledge"]:
        op.drop_column(table, "embedding_fingerprint", schema="argus")
    for name in ["publication_status", "publication_result", "publication_artifact"]:
        op.drop_column("reviews", name, schema="argus")
    op.drop_constraint("notes_mr_id_provider_note_id_provider_note_kind_key", "notes", schema="argus", type_="unique")
    op.drop_column("notes", "provider_note_kind", schema="argus")
    op.create_unique_constraint("notes_mr_id_provider_note_id_key", "notes", ["mr_id", "provider_note_id"], schema="argus")
    op.drop_constraint("mr_versions_mr_id_snapshot_key_key", "mr_versions", schema="argus", type_="unique")
    op.drop_column("mr_versions", "snapshot_key", schema="argus")
    op.alter_column("mr_versions", "provider_version_id", nullable=False, schema="argus")
    for name in ["embedding_config", "default_llm_endpoint_id", "provider_project_id"]:
        op.drop_column("repositories", name, schema="argus")
    op.alter_column("repositories", "gitlab_project_id", nullable=False, schema="argus")
