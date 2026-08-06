import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


async def up(engine: AsyncEngine):
    """
    Version: v2.7.0
    Add revisions table for the revision tree rollback system.

    Each row is an immutable snapshot node; parent_id links form a version tree.
    Backward compatible: changeset.json continues to work as the pending pool;
    revisions are committed only on approve/rollback.
    """
    is_postgres = "postgresql" in str(engine.url)

    async with engine.begin() as conn:
        if is_postgres:
            await conn.execute(text(
                """
                CREATE TABLE IF NOT EXISTS revisions (
                    id SERIAL PRIMARY KEY,
                    parent_id INTEGER REFERENCES revisions(id),
                    namespace VARCHAR(64) NOT NULL DEFAULT '',
                    changeset TEXT NOT NULL,
                    author VARCHAR(32) NOT NULL DEFAULT 'ai',
                    message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            ))
        else:
            await conn.execute(text(
                """
                CREATE TABLE IF NOT EXISTS revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_id INTEGER REFERENCES revisions(id),
                    namespace VARCHAR(64) NOT NULL DEFAULT '',
                    changeset TEXT NOT NULL,
                    author VARCHAR(32) NOT NULL DEFAULT 'ai',
                    message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            ))

    logger.info("Migration 016: created revisions table (revision tree rollback)")
