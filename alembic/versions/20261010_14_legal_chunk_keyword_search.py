"""Add keyword search to legal_chunks: search_text, tsvector + GIN, label lookup.

Revision ID: 20261010_14
Revises: 20261010_13

Someone searching asks things like "section 19A": they want an exact word/
number match, not just semantic similarity. This migration adds:

* ``legal_chunks.search_text`` -- the denormalized "Act title + section label
  + heading + text" string built by
  ``app.services.legal_chunking.build_search_text`` and persisted by
  ``LegalChunkRepository.build_for_revision`` for every row inserted from
  now on.
* ``legal_chunks.search_vector`` -- a database-maintained, generated
  ``tsvector`` column derived from ``search_text`` (``GENERATED ALWAYS AS``),
  so it can never drift out of sync with the text it indexes, and the
  application never writes to it directly.
* A GIN index on ``search_vector`` for fast full-text search.
* A btree index on ``(chunk_set_id, label)`` for exact, unstemmed lookup by
  printed label (e.g. "19A") scoped to one chunk-set build, so "Section 19"
  and "Section 19A" are never confused with one another.

Any ``legal_chunks`` rows already built by an earlier run of
``scripts/build_legal_chunks.py`` (before this migration existed) predate
``search_text``. ``chunk_text`` already carries the same Act-title/label/
heading/body content (it is ``"{prefix}\n\n{body}"``), so existing rows are
backfilled from it before the NOT NULL/nonblank constraint is enforced --
no need to re-chunk anything. New chunks built after this migration get
their ``search_text`` from ``LegalChunkRepository`` directly instead.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR


revision = "20261010_14"
down_revision = "20261010_13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "legal_chunks",
        sa.Column("search_text", sa.Text(), nullable=True),
    )
    # Backfill pre-existing rows (built before this migration existed) from
    # chunk_text, which already contains the Act title, label, heading and
    # body text in the same "{prefix}\n\n{body}" shape search_text uses.
    op.execute("UPDATE legal_chunks SET search_text = chunk_text WHERE search_text IS NULL")
    op.alter_column("legal_chunks", "search_text", nullable=False)
    op.create_check_constraint(
        "ck_legal_chunks_search_text_nonempty", "legal_chunks",
        "length(btrim(search_text)) > 0",
    )

    op.add_column(
        "legal_chunks",
        sa.Column(
            "search_vector",
            TSVECTOR(),
            sa.Computed("to_tsvector('english'::regconfig, search_text)", persisted=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_legal_chunks_search_vector", "legal_chunks", ["search_vector"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_legal_chunks_chunk_set_label", "legal_chunks", ["chunk_set_id", "label"],
    )


def downgrade() -> None:
    op.drop_index("ix_legal_chunks_chunk_set_label", table_name="legal_chunks")
    op.drop_index("ix_legal_chunks_search_vector", table_name="legal_chunks")
    op.drop_column("legal_chunks", "search_vector")
    op.drop_constraint("ck_legal_chunks_search_text_nonempty", "legal_chunks", type_="check")
    op.drop_column("legal_chunks", "search_text")
