import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import inspect as sa_inspect

logger = logging.getLogger(__name__)

async def up(engine: AsyncEngine):
    """
    Version: v2.6.0
    Add world_timestamp column to memories and search_documents tables.
    """
    
    def get_columns(connection, table_name):
        inspector = sa_inspect(connection)
        return [col["name"] for col in inspector.get_columns(table_name)]

    async with engine.begin() as conn:
        # 1. Update memories table
        memories_cols = await conn.run_sync(lambda sync_conn: get_columns(sync_conn, "memories"))
        if "world_timestamp" not in memories_cols:
            logger.info("Adding world_timestamp to memories table...")
            await conn.execute(text("ALTER TABLE memories ADD COLUMN world_timestamp TEXT"))

        # 2. Update search_documents table
        search_cols = await conn.run_sync(lambda sync_conn: get_columns(sync_conn, "search_documents"))
        if "world_timestamp" not in search_cols:
            logger.info("Adding world_timestamp to search_documents table...")
            await conn.execute(text("ALTER TABLE search_documents ADD COLUMN world_timestamp TEXT"))

    logger.info("Migration 015: world_timestamp columns verified/added.")
