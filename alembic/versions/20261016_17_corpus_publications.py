"""Add immutable publication membership; reuse existing GIN and HNSW indexes.

Revision ID: 20261016_17
Revises: 20261014_16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "20261016_17"
down_revision = "20261014_16"
branch_labels = None
depends_on = None


def upgrade():
    # Fail promptly instead of holding up ingestion during a busy deployment.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table("corpus_publications",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("profile_id", pg.UUID(as_uuid=True), sa.ForeignKey("embedding_profiles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("chunk_count", sa.Integer, nullable=False),
        sa.Column("validation_report", pg.JSONB, nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("name", name="uq_corpus_publications_name"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_publications_name"),
        sa.CheckConstraint("status IN ('draft','ready','active','retired')", name="ck_publications_status"),
        sa.CheckConstraint("manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_publications_hash"),
        sa.CheckConstraint("chunk_count > 0", name="ck_publications_count"),
        sa.CheckConstraint("status <> 'active' OR activated_at IS NOT NULL", name="ck_publications_activation"),
    )
    op.create_index("uq_corpus_publications_single_active", "corpus_publications", ["status"],
                    unique=True, postgresql_where=sa.text("status = 'active'"))
    op.create_table("publication_items",
        sa.Column("publication_id", pg.UUID(as_uuid=True), sa.ForeignKey("corpus_publications.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("chunk_set_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("revision_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("legal_version_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("chunk_count", sa.Integer, nullable=False),
        sa.ForeignKeyConstraint(["chunk_set_id", "revision_id"], ["chunk_sets.id", "chunk_sets.revision_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["revision_id", "legal_version_id"], ["corpus_revisions.id", "corpus_revisions.legal_version_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["legal_version_id", "instrument_id"], ["legal_versions.id", "legal_versions.instrument_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("publication_id", "instrument_id", name="uq_publication_items_instrument"),
        sa.UniqueConstraint("publication_id", "revision_id", name="uq_publication_items_revision"),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_publication_items_hash"),
        sa.CheckConstraint("chunk_count > 0", name="ck_publication_items_count"),
    )
    op.create_index("ix_publication_items_chunk_set", "publication_items", ["chunk_set_id"])
    op.execute("""
        CREATE FUNCTION legalmind_guard_publication() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE publication_state text;
        BEGIN
          IF TG_TABLE_NAME = 'publication_items' THEN
            IF TG_OP='UPDATE' AND OLD.publication_id IS DISTINCT FROM NEW.publication_id THEN
              RAISE EXCEPTION 'Publication membership cannot be moved';
            END IF;
            SELECT status INTO publication_state FROM corpus_publications
              WHERE id = CASE WHEN TG_OP='DELETE' THEN OLD.publication_id ELSE NEW.publication_id END;
            IF publication_state <> 'draft' THEN
              RAISE EXCEPTION 'Publication membership is sealed; create a new publication';
            END IF;
          ELSE
            IF TG_OP='DELETE' AND OLD.status <> 'draft' THEN
              RAISE EXCEPTION 'Sealed publications are retained for history';
            END IF;
            IF TG_OP='UPDATE' AND OLD.status <> 'draft' AND
               (to_jsonb(OLD) - 'status' - 'activated_at') IS DISTINCT FROM
               (to_jsonb(NEW) - 'status' - 'activated_at') THEN
              RAISE EXCEPTION 'Publication manifest is immutable';
            END IF;
            IF TG_OP='UPDATE' AND OLD.status <> 'draft' AND NEW.status='draft' THEN
              RAISE EXCEPTION 'A sealed publication cannot become a draft';
            END IF;
            IF TG_OP='UPDATE' AND NEW.status IN ('ready','active') THEN
              IF NOT EXISTS(SELECT 1 FROM publication_items WHERE publication_id=NEW.id) OR
                 (SELECT coalesce(sum(chunk_count),0) FROM publication_items WHERE publication_id=NEW.id) <> NEW.chunk_count OR
                 EXISTS(SELECT 1 FROM publication_items pi JOIN corpus_revisions r ON r.id=pi.revision_id
                   JOIN chunk_sets cs ON cs.id=pi.chunk_set_id WHERE pi.publication_id=NEW.id AND
                   (r.review_state <> 'approved' OR r.content_sha256 IS DISTINCT FROM pi.content_sha256
                    OR cs.content_sha256 IS DISTINCT FROM pi.content_sha256 OR
                    (SELECT count(*) FROM legal_chunks c JOIN legal_chunk_embeddings e ON e.chunk_id=c.id
                      AND e.profile_id=NEW.profile_id WHERE c.chunk_set_id=pi.chunk_set_id) <> pi.chunk_count)) THEN
                RAISE EXCEPTION 'A publication must be complete and approved before sealing or activation';
              END IF;
            END IF;
          END IF;
          IF TG_OP='DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER guard_publication BEFORE UPDATE OR DELETE ON corpus_publications "
               "FOR EACH ROW EXECUTE FUNCTION legalmind_guard_publication()")
    op.execute("CREATE TRIGGER guard_publication_items BEFORE INSERT OR UPDATE OR DELETE ON publication_items "
               "FOR EACH ROW EXECUTE FUNCTION legalmind_guard_publication()")
    # Freezes the data actually referenced by a publication, not unrelated new
    # revisions, new profiles, ingestion jobs, uploads or user contracts.
    op.execute("""
        CREATE FUNCTION legalmind_guard_published_data() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE pinned boolean := false;
                data jsonb;
        BEGIN
          data := CASE WHEN TG_OP='INSERT' THEN to_jsonb(NEW) ELSE to_jsonb(OLD) END;
          IF TG_OP='UPDATE' AND to_jsonb(OLD)=to_jsonb(NEW) THEN RETURN NEW; END IF;
          CASE TG_TABLE_NAME
            WHEN 'legal_chunks' THEN
              SELECT EXISTS(SELECT 1 FROM publication_items WHERE chunk_set_id=(data->>'chunk_set_id')::uuid
                OR (TG_OP='UPDATE' AND chunk_set_id=(to_jsonb(NEW)->>'chunk_set_id')::uuid)) INTO pinned;
            WHEN 'chunk_sets' THEN
              SELECT EXISTS(SELECT 1 FROM publication_items WHERE chunk_set_id=(data->>'id')::uuid) INTO pinned;
            WHEN 'legal_chunk_provisions' THEN
              SELECT EXISTS(SELECT 1 FROM publication_items pi JOIN legal_chunks c ON c.chunk_set_id=pi.chunk_set_id
                WHERE c.id=(data->>'chunk_id')::uuid OR
                (TG_OP='UPDATE' AND c.id=(to_jsonb(NEW)->>'chunk_id')::uuid)) INTO pinned;
            WHEN 'legal_chunk_embeddings' THEN
              SELECT EXISTS(SELECT 1 FROM publication_items pi JOIN corpus_publications p ON p.id=pi.publication_id
                JOIN legal_chunks c ON c.chunk_set_id=pi.chunk_set_id
                WHERE c.id=(data->>'chunk_id')::uuid AND p.profile_id=(data->>'profile_id')::uuid) INTO pinned;
            WHEN 'embedding_profiles' THEN
              SELECT EXISTS(SELECT 1 FROM legal_chunk_embeddings WHERE profile_id=(data->>'id')::uuid) INTO pinned;
            WHEN 'provisions', 'provision_spans', 'revision_sources' THEN
              SELECT EXISTS(SELECT 1 FROM publication_items WHERE revision_id=(data->>'revision_id')::uuid
                OR (TG_OP='UPDATE' AND revision_id=(to_jsonb(NEW)->>'revision_id')::uuid)) INTO pinned;
            WHEN 'legal_instruments' THEN
              SELECT EXISTS(SELECT 1 FROM publication_items WHERE instrument_id=(data->>'id')::uuid) INTO pinned;
            WHEN 'legal_versions' THEN
              SELECT EXISTS(SELECT 1 FROM publication_items WHERE legal_version_id=(data->>'id')::uuid) INTO pinned;
            WHEN 'corpus_revisions' THEN
              IF TG_OP='UPDATE' AND
                 (to_jsonb(OLD)-'review_state'-'approved_by'-'approved_at') =
                 (to_jsonb(NEW)-'review_state'-'approved_by'-'approved_at') THEN RETURN NEW; END IF;
              SELECT EXISTS(SELECT 1 FROM publication_items WHERE revision_id=(data->>'id')::uuid) INTO pinned;
            WHEN 'file_extractions' THEN
              SELECT EXISTS(SELECT 1 FROM revision_sources rs JOIN publication_items pi ON pi.revision_id=rs.revision_id
                WHERE rs.extraction_id=(data->>'id')::uuid) INTO pinned;
            WHEN 'file_pages' THEN
              SELECT EXISTS(SELECT 1 FROM revision_sources rs JOIN publication_items pi ON pi.revision_id=rs.revision_id
                WHERE rs.extraction_id=(data->>'extraction_id')::uuid) INTO pinned;
            WHEN 'file_blocks' THEN
              SELECT EXISTS(SELECT 1 FROM provision_spans ps JOIN publication_items pi ON pi.revision_id=ps.revision_id
                WHERE ps.block_id=(data->>'id')::uuid) INTO pinned;
            WHEN 'legal_files' THEN
              SELECT EXISTS(SELECT 1 FROM revision_sources rs JOIN publication_items pi ON pi.revision_id=rs.revision_id
                WHERE rs.file_id=(data->>'id')::uuid) INTO pinned;
          END CASE;
          IF pinned THEN RAISE EXCEPTION 'Published corpus data is immutable; create a new revision or profile'; END IF;
          IF TG_OP='DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END $$
    """)
    for table in PINNED_TABLES:
        op.execute(f"CREATE TRIGGER guard_published_data BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION legalmind_guard_published_data()")


PINNED_TABLES = (
    "legal_chunks", "chunk_sets", "legal_chunk_provisions", "legal_chunk_embeddings",
    "embedding_profiles", "provisions", "provision_spans", "revision_sources",
    "legal_instruments", "legal_versions", "corpus_revisions", "file_extractions",
    "file_pages", "file_blocks", "legal_files",
)


def downgrade():
    for table in PINNED_TABLES:
        op.execute(f"DROP TRIGGER guard_published_data ON {table}")
    op.execute("DROP FUNCTION legalmind_guard_published_data()")
    op.execute("DROP TRIGGER guard_publication_items ON publication_items")
    op.execute("DROP TRIGGER guard_publication ON corpus_publications")
    op.execute("DROP FUNCTION legalmind_guard_publication()")
    op.drop_table("publication_items")
    op.drop_table("corpus_publications")
