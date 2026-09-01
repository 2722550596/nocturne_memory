import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


async def up(engine: AsyncEngine):
    """
    Version: v2.8.0
    Add search_document_embeddings table for semantic (vector) search.

    One row per embedded segment of a search document.  Vectors are stored as
    TEXT JSON arrays on purpose - no sqlite-vec / pgvector extension is
    introduced; scoring happens in-process with numpy over the loaded rows.
    No ForeignKey to search_documents (whose PK is composite and has no
    autoincrement id): consistency is maintained by code-level hash
    reconciliation (md5(content|search_terms)) plus deleting stale rows on
    re-embed, so deleted documents never leave scoring rows behind.

    SQLite and PostgreSQL share the same DDL (no SERIAL / AUTOINCREMENT
    needed - the PK is a composite of VARCHAR/INTEGER columns).
    """
    is_postgres = "postgresql" in str(engine.url)

    async with engine.begin() as conn:
        await conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS search_document_embeddings (
                namespace VARCHAR(64) NOT NULL DEFAULT '',
                domain VARCHAR(64) NOT NULL DEFAULT 'core',
                path VARCHAR(512) NOT NULL,
                seg_index INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                vector TEXT NOT NULL,
                model VARCHAR(128) NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (namespace, domain, path, seg_index)
            )
            """
        ))

    if not is_postgres:
        logger.info("Migration 017: created search_document_embeddings table (semantic search)")
    else:
        logger.info("Migration 017: created search_document_embeddings table (semantic search, postgres)")
