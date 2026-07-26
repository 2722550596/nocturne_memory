# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportOperatorIssue=false

"""
FTS Search Indexer and Query Engine for Nocturne Memory System.

Maintains derived search rows (search_documents / search_documents_fts)
and provides full-text search across the memory graph.
"""

from typing import Optional, Dict, Any, List, TYPE_CHECKING

from sqlalchemy import select, delete, text, or_
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    Memory,
    Edge,
    Path,
    GlossaryKeyword,
    SearchDocument,
    escape_like_literal,
)
from .search_terms import build_document_search_terms, expand_query_terms

if TYPE_CHECKING:
    from .database import DatabaseManager


class SearchIndexer:
    """FTS index maintenance and query engine.

    Manages the derived search_documents table, keeping it in sync with
    the live graph state.  Supports both SQLite FTS5 and PostgreSQL
    tsvector backends.
    """

    def __init__(self, db: "DatabaseManager"):
        self._session = db.session
        self._optional_session = db._optional_session
        self.db_type = db.db_type

    # -----------------------------------------------------------------
    # Query helpers (stateless)
    # -----------------------------------------------------------------

    @staticmethod
    def _to_sqlite_match_query(query: str, *, use_or: bool = False) -> str:
        """Convert free text into an FTS5 MATCH expression.

        use_or=False (default) produces an AND query for strict matching.
        use_or=True produces an OR query for fallback recall.
        """
        normalized = expand_query_terms(query)
        tokens = [token.replace('"', '""') for token in normalized.split() if token]
        if not tokens:
            raw = query.strip().replace('"', '""')
            return f'"{raw}"' if raw else ""
        op = " OR " if use_or else " AND "
        return op.join(f'"{token}"' for token in tokens)

    @staticmethod
    def _to_or_query(query: str) -> str:
        """Build an OR-separated token string for PostgreSQL websearch_to_tsquery."""
        normalized = expand_query_terms(query)
        tokens = [t for t in normalized.split() if t]
        return " OR ".join(tokens)

    @staticmethod
    def _format_search_snippet(content: str, query: str) -> str:
        """Build a short content snippet around the first literal hit or token hit."""
        if not content:
            return ""

        content_lower = content.lower()
        query_lower = query.lower()

        pos = content_lower.find(query_lower)
        match_len = len(query)

        if pos < 0:
            tokens = expand_query_terms(query).split()
            for token in tokens:
                if not token:
                    continue
                pos = content_lower.find(token.lower())
                if pos >= 0:
                    match_len = len(token)
                    break

        if pos < 0:
            fallback = content[:80]
            return fallback + ("..." if len(content) > 80 else "")

        start = max(0, pos - 30)
        end = min(len(content), pos + match_len + 30)
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(content) else ""
        return prefix + content[start:end] + suffix

    # -----------------------------------------------------------------
    # Index maintenance
    # -----------------------------------------------------------------

    async def _build_search_documents_for_node(
        self, session: AsyncSession, node_uuid: str, *, namespace: str = "", search_all_namespaces: bool = False
    ) -> List[Dict[str, Any]]:
        """Materialize search rows for every reachable path of a node.

        When search_all_namespaces is True, builds for ALL namespaces.
        Otherwise only builds docs for paths in the specified namespace.
        """
        memory = (
            await session.execute(
                select(Memory)
                .where(Memory.node_uuid == node_uuid, Memory.deprecated == False)
                .limit(1)
            )
        ).scalar_one_or_none()
        if not memory:
            return []

        path_stmt = (
            select(Path.namespace, Path.domain, Path.path, Edge.priority, Edge.disclosure)
            .select_from(Path)
            .join(Edge, Path.edge_id == Edge.id)
            .where(Path.node_uuid == node_uuid)
        )
        if not search_all_namespaces:
            path_stmt = path_stmt.where(Path.namespace == namespace)
        path_stmt = path_stmt.order_by(Path.domain, Path.path)
        path_rows = (await session.execute(path_stmt)).all()
        if not path_rows:
            return []

        keyword_stmt = select(GlossaryKeyword.keyword, GlossaryKeyword.namespace).where(
            GlossaryKeyword.node_uuid == node_uuid
        )
        if not search_all_namespaces:
            keyword_stmt = keyword_stmt.where(GlossaryKeyword.namespace == namespace)

        keyword_rows = await session.execute(keyword_stmt)
        
        from collections import defaultdict
        keywords_by_ns = defaultdict(list)
        for kw, ns in keyword_rows:
            if kw:
                keywords_by_ns[ns].append(kw)

        documents = []
        for row in path_rows:
            uri = f"{row.domain}://{row.path}"
            ns_keywords = keywords_by_ns.get(row.namespace, [])
            glossary_text = " ".join(sorted(ns_keywords))
            documents.append(
                {
                    "namespace": row.namespace,
                    "domain": row.domain,
                    "path": row.path,
                    "node_uuid": node_uuid,
                    "memory_id": memory.id,
                    "uri": uri,
                    "content": memory.content,
                    "disclosure": row.disclosure,
                    "search_terms": build_document_search_terms(
                        row.path,
                        uri,
                        memory.content,
                        row.disclosure,
                        glossary_text,
                    ),
                    "priority": row.priority,
                }
            )
        return documents

    async def _delete_search_documents_for_node(
        self, session: AsyncSession, node_uuid: str, *, namespace: str = "", search_all_namespaces: bool = False
    ) -> None:
        """Remove derived search rows for a node.

        When search_all_namespaces is True, removes ALL namespaces.
        Otherwise only removes docs in the specified namespace.
        """
        if not search_all_namespaces:
            if self.db_type == "sqlite":
                await session.execute(
                    text(
                        "DELETE FROM search_documents_fts "
                        "WHERE node_uuid = :node_uuid AND namespace = :namespace"
                    ),
                    {"node_uuid": node_uuid, "namespace": namespace},
                )
            await session.execute(
                delete(SearchDocument).where(
                    SearchDocument.node_uuid == node_uuid,
                    SearchDocument.namespace == namespace,
                )
            )
        else:
            if self.db_type == "sqlite":
                await session.execute(
                    text("DELETE FROM search_documents_fts WHERE node_uuid = :node_uuid"),
                    {"node_uuid": node_uuid},
                )
            await session.execute(
                delete(SearchDocument).where(SearchDocument.node_uuid == node_uuid)
            )

    async def _insert_search_documents(
        self, session: AsyncSession, documents: List[Dict[str, Any]]
    ) -> None:
        """Insert fresh derived search rows for one node."""
        if not documents:
            return

        session.add_all(SearchDocument(**doc) for doc in documents)
        await session.flush()

        if self.db_type != "sqlite":
            return

        for doc in documents:
            await session.execute(
                text(
                    """
                    INSERT INTO search_documents_fts (
                        namespace, domain, path, node_uuid, uri, content, disclosure, search_terms
                    ) VALUES (
                        :namespace, :domain, :path, :node_uuid, :uri, :content, coalesce(:disclosure, ''), :search_terms
                    )
                    """
                ),
                doc,
            )

    async def refresh_search_documents_for_node(
        self, node_uuid: str, session: Optional[AsyncSession] = None, namespace: str = "", refresh_all_namespaces: bool = False
    ) -> None:
        """Rebuild derived search rows for one node in the given namespace (defaults to current)."""
        async with self._optional_session(session) as session:
            documents = await self._build_search_documents_for_node(
                session, node_uuid, namespace=namespace, search_all_namespaces=refresh_all_namespaces
            )
            await self._delete_search_documents_for_node(
                session, node_uuid, namespace=namespace, search_all_namespaces=refresh_all_namespaces
            )
            await self._insert_search_documents(session, documents)

    async def get_node_uuids_for_prefix(
        self, session: AsyncSession, domain: str, base_path: str, namespace: str = ""
    ) -> List[str]:
        """Collect unique node UUIDs for a path and all descendants."""
        safe = escape_like_literal(base_path)
        result = await session.execute(
            select(Path.node_uuid)
            .where(Path.namespace == namespace)
            .where(Path.domain == domain)
            .where(
                or_(
                    Path.path == base_path,
                    Path.path.like(f"{safe}/%", escape="\\"),
                )
            )
            .distinct()
        )
        return [row[0] for row in result.all()]

    async def rebuild_all_search_documents(
        self, session: Optional[AsyncSession] = None
    ) -> None:
        """Fully rebuild the derived search index from live graph state (all namespaces)."""
        async with self._optional_session(session) as session:
            if self.db_type == "sqlite":
                await session.execute(text("DELETE FROM search_documents_fts"))

            await session.execute(delete(SearchDocument))

            result = await session.execute(
                select(Path.node_uuid).distinct()
            )
            for (node_uuid,) in result.all():
                documents = await self._build_search_documents_for_node(
                    session, node_uuid, search_all_namespaces=True
                )
                await self._insert_search_documents(session, documents)

    # -----------------------------------------------------------------
    # Public search API
    # -----------------------------------------------------------------

    async def _search_pass(
        self,
        session,
        query: str,
        *,
        use_or: bool = False,
        limit: int = 10,
        domain: Optional[str] = None,
        namespace: str = "",
    ) -> List[Dict[str, Any]]:
        """Single-pass search. Returns formatted match dicts (may include dupes across passes)."""
        candidate_limit = max(limit * 5, 50)
        params: Dict[str, Any] = {
            "candidate_limit": candidate_limit,
            "namespace": namespace,
        }
        domain_clause = ""
        if domain is not None:
            params["domain"] = domain
            domain_clause = "AND sd.domain = :domain"

        if self.db_type == "sqlite":
            match_query = self._to_sqlite_match_query(query, use_or=use_or)
            if not match_query:
                return []

            params["match_query"] = match_query
            result = await session.execute(
                text(
                    f"""
                    SELECT
                        sd.domain,
                        sd.path,
                        sd.node_uuid,
                        sd.uri,
                        sd.priority,
                        sd.content,
                        sd.disclosure,
                        bm25(search_documents_fts, 0.0, 0.0, 2.5, 0.0, 2.0, 1.0, 1.0, 0.75) AS score
                    FROM search_documents AS sd
                    JOIN search_documents_fts
                      ON search_documents_fts.namespace = sd.namespace
                     AND search_documents_fts.domain = sd.domain
                     AND search_documents_fts.path = sd.path
                    WHERE search_documents_fts MATCH :match_query
                      AND sd.namespace = :namespace
                      {domain_clause}
                    ORDER BY score ASC, sd.priority ASC, length(sd.path) ASC
                    LIMIT :candidate_limit
                    """
                ),
                params,
            )
        else:
            if use_or:
                ts_query_str = self._to_or_query(query)
            else:
                ts_query_str = expand_query_terms(query)
            if not ts_query_str:
                return []

            params["ts_query"] = ts_query_str
            # Both passes use websearch_to_tsquery:
            # AND pass: unquoted spaces → implicit AND
            # OR pass:  explicit "OR" keyword between tokens
            result = await session.execute(
                text(
                    f"""
                    SELECT
                        sd.domain,
                        sd.path,
                        sd.node_uuid,
                        sd.uri,
                        sd.priority,
                        sd.content,
                        sd.disclosure,
                        ts_rank_cd(
                            to_tsvector(
                                'simple',
                                coalesce(sd.path, '') || ' ' ||
                                coalesce(sd.uri, '') || ' ' ||
                                coalesce(sd.content, '') || ' ' ||
                                coalesce(sd.disclosure, '') || ' ' ||
                                coalesce(sd.search_terms, '')
                            ),
                            websearch_to_tsquery('simple', :ts_query)
                        ) AS score
                    FROM search_documents AS sd
                    WHERE sd.namespace = :namespace
                      AND to_tsvector(
                            'simple',
                            coalesce(sd.path, '') || ' ' ||
                            coalesce(sd.uri, '') || ' ' ||
                            coalesce(sd.content, '') || ' ' ||
                            coalesce(sd.disclosure, '') || ' ' ||
                            coalesce(sd.search_terms, '')
                          ) @@ websearch_to_tsquery('simple', :ts_query)
                      {domain_clause}
                    ORDER BY score DESC, sd.priority ASC, char_length(sd.path) ASC
                    LIMIT :candidate_limit
                    """
                ),
                params,
            )

        rows = []
        for row in result.mappings():
            rows.append(
                {
                    "domain": row["domain"],
                    "path": row["path"],
                    "node_uuid": row["node_uuid"],
                    "uri": row["uri"],
                    "name": row["path"].rsplit("/", 1)[-1],
                    "snippet": self._format_search_snippet(row["content"], query),
                    "priority": row["priority"],
                    "disclosure": row["disclosure"],
                    "score": float(row["score"]),
                }
            )
        return rows

    async def search(
        self, query: str, limit: int = 10, domain: Optional[str] = None, namespace: str = ""
    ) -> List[Dict[str, Any]]:
        """Search memories with AND-first, OR-fallback strategy.

        1. Strict AND pass — all query tokens must appear. Highest precision.
        2. If AND yields fewer than *limit* results, run an OR pass and
           merge, keeping AND results at the top.
        """
        async with self._session() as session:
            # Pass 1: AND (strict)
            and_rows = await self._search_pass(
                session, query, use_or=False, limit=limit, domain=domain, namespace=namespace
            )

            matches: List[Dict[str, Any]] = []
            seen_nodes: set = set()

            for row in and_rows:
                if row["node_uuid"] in seen_nodes:
                    continue
                seen_nodes.add(row["node_uuid"])
                matches.append({k: v for k, v in row.items() if k != "score"})
                if len(matches) >= limit:
                    return matches

            # Pass 2: OR (fallback) — fill remaining slots
            or_rows = await self._search_pass(
                session, query, use_or=True, limit=limit, domain=domain, namespace=namespace
            )

            for row in or_rows:
                if row["node_uuid"] in seen_nodes:
                    continue
                seen_nodes.add(row["node_uuid"])
                matches.append({k: v for k, v in row.items() if k != "score"})
                if len(matches) >= limit:
                    break

            return matches
