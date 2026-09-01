"""
Embedding pipeline for semantic search (``search_memory(semantic=True)``).

Reuses the algorithm validated in ``install/extensions/nocturne-memory-recall.ts``:
embed call format, batching, L2 normalization, chunking, content-hash
reconciliation, and max-segment cosine.  Vectors live in the main database's
``search_document_embeddings`` table (TEXT JSON, no sqlite-vec/pgvector) — the
recall extension keeps its own ``recall-cache/embeddings.sqlite`` untouched; the
injection channel and the search channel stay independent.

Every public method degrades gracefully: any API failure returns ``None`` /
empty results so the caller falls back to lexical search.
"""

import hashlib
import json
import logging
import time
from typing import Dict, List, Optional, Tuple

import httpx

from config import get_embedding_config

logger = logging.getLogger(__name__)

# Composite key of search_documents / search_document_embeddings.
_DocKey = Tuple[str, str, str]


def _content_hash(content: str, search_terms: str) -> str:
    """Hash identifying one version of a search document's embeddable text.

    Mirrors the recall extension: md5 of ``content|search_terms``.  Changing
    either invalidates the stored segments and triggers re-embedding.
    """
    return hashlib.md5(f"{content}|{search_terms}".encode("utf-8")).hexdigest()


class EmbeddingService:
    """Embedding client + vector store reconciliation for semantic search."""

    def __init__(self, db):
        self._db = db

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    @staticmethod
    def _config() -> dict:
        return get_embedding_config()

    def enabled(self) -> bool:
        """True when an embedding API key is configured (semantic usable)."""
        return bool(self._config().get("api_key"))

    # ------------------------------------------------------------------
    # Text chunking (mirrors chunkText in the recall extension)
    # ------------------------------------------------------------------

    def chunk_text(self, text: str) -> List[str]:
        """Split text into overlapping segments; empty input returns [""]."""
        cfg = self._config()
        max_len = int(cfg.get("max_input_chars", 500))
        overlap = int(cfg.get("chunk_overlap", 80))
        if not text:
            return [""]
        if len(text) <= max_len:
            return [text]
        chunks: List[str] = []
        step = max_len - overlap
        for start in range(0, len(text), step):
            chunks.append(text[start:start + max_len])
        return chunks

    # ------------------------------------------------------------------
    # Embedding client
    # ------------------------------------------------------------------

    async def embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        """Embed texts in batches; None on any failure (caller degrades).

        Batch size, request shape, 30s timeout and the normalize step all
        mirror the validated recall extension implementation.
        """
        if not texts:
            return []
        cfg = self._config()
        api_key = cfg.get("api_key") or ""
        if not api_key:
            return None
        model = cfg.get("model") or "BAAI/bge-large-zh-v1.5"
        api_url = (cfg.get("api_url") or "https://api.siliconflow.cn/v1").rstrip("/")
        batch_size = int(cfg.get("batch_size", 32))

        vectors: List[List[float]] = []
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                for i in range(0, len(texts), batch_size):
                    batch = texts[i:i + batch_size]
                    resp = await client.post(
                        f"{api_url}/embeddings",
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {api_key}",
                        },
                        json={"model": model, "input": batch},
                    )
                    if resp.status_code != 200:
                        logger.warning("embedding API returned %s", resp.status_code)
                        return None
                    payload = resp.json()
                    data = payload.get("data") if isinstance(payload, dict) else None
                    if not isinstance(data, list) or len(data) != len(batch):
                        logger.warning("embedding API returned %d items for %d inputs", len(data) if isinstance(data, list) else -1, len(batch))
                        return None
                    for item in data:
                        emb = item.get("embedding") if isinstance(item, dict) else None
                        if not isinstance(emb, list):
                            return None
                        vectors.append(self._normalize([float(x) for x in emb]))
        except Exception:
            logger.warning("embedding API call failed", exc_info=True)
            return None
        return vectors

    @staticmethod
    def _normalize(v: List[float]) -> List[float]:
        """L2-normalize a vector; returns a new list (input untouched)."""
        norm = sum(x * x for x in v) ** 0.5
        if norm <= 0:
            return v
        return [x / norm for x in v]

    async def embed_query(self, query: str) -> Optional[List[float]]:
        """Embed a search query (truncated to max_input_chars); None on failure."""
        cfg = self._config()
        max_len = int(cfg.get("max_input_chars", 500))
        vecs = await self.embed([query[:max_len]])
        if not vecs:
            return None
        return vecs[0]

    # ------------------------------------------------------------------
    # Vector store reconciliation
    # ------------------------------------------------------------------

    async def ensure_documents_embeddings(self, docs: List[dict]) -> None:
        """Embed missing/outdated segments for *docs* and upsert them.

        *docs* are the dicts produced by
        ``SearchIndexer._build_search_documents_for_node`` (namespace/domain/
        path/content/disclosure/search_terms/...).  A doc is skipped when every
        stored segment's content_hash matches md5(content|search_terms);
        otherwise its stale rows are deleted and fresh segments are embedded.
        API failures leave old rows in place (retried on the next refresh).
        Runs in its own session/transaction — never borrows the caller's.
        """
        if not docs or not self.enabled():
            return

        cfg = self._config()
        model = cfg.get("model") or "BAAI/bge-large-zh-v1.5"

        # Group docs by (namespace, domain) to keep the cache read narrow.
        by_scope: Dict[Tuple[str, str], List[dict]] = {}
        for doc in docs:
            key = (doc.get("namespace", ""), doc.get("domain", "core"))
            by_scope.setdefault(key, []).append(doc)

        from sqlalchemy import delete, select

        from .models import SearchDocumentEmbedding

        async with self._db.session() as session:
            for (ns, domain), scope_docs in by_scope.items():
                rows = (
                    await session.execute(
                        select(
                            SearchDocumentEmbedding.namespace,
                            SearchDocumentEmbedding.domain,
                            SearchDocumentEmbedding.path,
                            SearchDocumentEmbedding.seg_index,
                            SearchDocumentEmbedding.content_hash,
                        ).where(
                            SearchDocumentEmbedding.namespace == ns,
                            SearchDocumentEmbedding.domain == domain,
                        )
                    )
                ).all()
                stored: Dict[_DocKey, List[Tuple[int, str]]] = {}
                for r in rows:
                    stored.setdefault((r.namespace, r.domain, r.path), []).append((r.seg_index, r.content_hash))

                # Split scope docs into fresh (all segments hash-match) and
                # to_embed (missing or stale).
                to_embed: List[dict] = []
                for doc in scope_docs:
                    key = (doc.get("namespace", ""), doc.get("domain", "core"), doc.get("path", ""))
                    doc_hash = _content_hash(doc.get("content", "") or "", doc.get("search_terms", "") or "")
                    segs = stored.get(key, [])
                    if segs and all(h == doc_hash for _, h in segs):
                        continue  # already fresh
                    to_embed.append(doc)

                if not to_embed:
                    continue

                # Delete stale rows for keys being re-embedded (replaced
                # wholesale below; missing keys have nothing to delete).
                for doc in to_embed:
                    key = (doc.get("namespace", ""), doc.get("domain", "core"), doc.get("path", ""))
                    if key in stored:
                        await session.execute(
                            delete(SearchDocumentEmbedding).where(
                                SearchDocumentEmbedding.namespace == key[0],
                                SearchDocumentEmbedding.domain == key[1],
                                SearchDocumentEmbedding.path == key[2],
                            )
                        )

                # Build segment texts (uri + disclosure + content — same shape
                # as the recall extension) and embed in one batched call.
                segs_by_key: Dict[_DocKey, List[str]] = {}
                seg_texts: List[str] = []
                for doc in to_embed:
                    uri = doc.get("uri", "")
                    disclosure = doc.get("disclosure") or ""
                    content = doc.get("content", "") or ""
                    segs = self.chunk_text(f"{uri}\n{disclosure}\n{content}")
                    key = (doc.get("namespace", ""), doc.get("domain", "core"), doc.get("path", ""))
                    segs_by_key[key] = segs
                    seg_texts.extend(segs)

                vectors = await self.embed(seg_texts)
                if vectors is None:
                    # API failure: leave rows as they were; retried later.
                    return
                if len(vectors) != len(seg_texts):
                    logger.warning(
                        "embedding response length mismatch (%d != %d)", len(vectors), len(seg_texts)
                    )
                    return

                now_ms = int(time.time() * 1000)
                vi = 0
                for doc in to_embed:
                    key = (doc.get("namespace", ""), doc.get("domain", "core"), doc.get("path", ""))
                    doc_hash = _content_hash(doc.get("content", "") or "", doc.get("search_terms", "") or "")
                    for si in range(len(segs_by_key[key])):
                        vec = vectors[vi]
                        vi += 1
                        session.add(
                            SearchDocumentEmbedding(
                                namespace=key[0],
                                domain=key[1],
                                path=key[2],
                                seg_index=si,
                                content_hash=doc_hash,
                                vector=json.dumps(vec, ensure_ascii=False),
                                model=model,
                                updated_at=now_ms,
                            )
                        )
                await session.flush()

    # ------------------------------------------------------------------
    # Semantic scoring
    # ------------------------------------------------------------------

    async def load_vectors(
        self, namespace: str, docs: List[dict]
    ) -> Dict[_DocKey, List[List[float]]]:
        """Load segment vectors whose content_hash still matches *docs*.

        Returns {key: [vectors...]} with segments ordered by seg_index.  Keys
        whose stored hash is stale (or that have no stored rows) are omitted —
        the caller schedules them for background backfill.
        """
        out: Dict[_DocKey, List[List[float]]] = {}
        if not docs:
            return out

        from sqlalchemy import select

        from .models import SearchDocumentEmbedding

        hashes = {
            (d.get("namespace", ""), d.get("domain", "core"), d.get("path", "")): _content_hash(
                d.get("content", "") or "", d.get("search_terms", "") or ""
            )
            for d in docs
        }
        namespaces = {d.get("namespace", "") for d in docs}

        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(
                        SearchDocumentEmbedding.namespace,
                        SearchDocumentEmbedding.domain,
                        SearchDocumentEmbedding.path,
                        SearchDocumentEmbedding.seg_index,
                        SearchDocumentEmbedding.content_hash,
                        SearchDocumentEmbedding.vector,
                    ).where(SearchDocumentEmbedding.namespace.in_(namespaces))
                )
            ).all()

        for r in rows:
            key = (r.namespace, r.domain, r.path)
            if key not in hashes or r.content_hash != hashes[key]:
                continue  # stale or foreign row
            try:
                vec = [float(x) for x in json.loads(r.vector)]
            except (ValueError, TypeError):
                continue
            bucket = out.setdefault(key, [])
            while len(bucket) <= r.seg_index:
                bucket.append(None)
            bucket[r.seg_index] = vec
        return {k: [v for v in vs if v is not None] for k, vs in out.items()}

    def cosine_matrix(
        self, query_vec: List[float], doc_vectors: Dict[_DocKey, List[List[float]]]
    ) -> Dict[_DocKey, float]:
        """Cosine similarity (max over segments) for every doc.

        All vectors are L2-normalized so cosine == dot product; numpy handles
        the matrix multiply.  Returns {} when numpy is missing or inputs are
        empty (caller falls back to lexical).
        """
        out: Dict[_DocKey, float] = {}
        if not query_vec or not doc_vectors:
            return out
        try:
            import numpy as np
        except ImportError:
            logger.warning("numpy not installed; semantic scoring disabled")
            return out
        q = np.asarray(query_vec, dtype=np.float64)
        for key, segs in doc_vectors.items():
            if not segs:
                continue
            try:
                matrix = np.asarray(segs, dtype=np.float64)
                sims = matrix @ q
                out[key] = float(sims.max())
            except Exception:
                continue
        return out
