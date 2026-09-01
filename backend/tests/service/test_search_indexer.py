async def test_search_indexer_refreshes_on_content_and_alias_changes(graph_service, search_indexer):
    await graph_service.create_memory(
        parent_path="",
        content="GraphService owns alias refreshes",
        priority=2,
        title="search_note",
        disclosure="When testing search refresh",
    )

    initial_results = await search_indexer.search("GraphService")

    await graph_service.update_memory("search_note", content="MemoryBrowser owns rendered search")
    await graph_service.add_path(
        new_path="mirrored_search_note",
        target_path="search_note",
        new_domain="project",
        target_domain="core",
        priority=4,
        disclosure="When mirroring search note",
    )

    updated_results = await search_indexer.search("MemoryBrowser")
    alias_results = await search_indexer.search("mirrored_search_note", domain="project")

    assert initial_results[0]["uri"] == "core://search_note"
    assert updated_results[0]["uri"] == "core://search_note"
    assert alias_results[0]["uri"] == "project://mirrored_search_note"


# =============================================================================
# Semantic search (search_memory(semantic=True))
# =============================================================================

import asyncio
import hashlib


class _FakeEmbedder:
    """Deterministic embedding stand-in: no network, configurable cosines.

    score_by_path maps a search-document path to its max-segment cosine.
    Records every ensure_documents_embeddings call and the content hashes it
    saw, so tests can assert backfill triggers and cache invalidation.
    """

    def __init__(self, score_by_path=None):
        self.score_by_path = score_by_path or {}
        self.ensure_calls = 0
        self.hashes = {}  # (namespace, domain, path) -> content_hash

    def enabled(self):
        return True

    async def ensure_documents_embeddings(self, docs):
        self.ensure_calls += 1
        for d in docs:
            key = (d.get("namespace", ""), d.get("domain", "core"), d.get("path", ""))
            raw = f"{d.get('content', '')}|{d.get('search_terms', '')}"
            self.hashes[key] = hashlib.md5(raw.encode("utf-8")).hexdigest()

    async def embed_query(self, query):
        return [0.1, 0.2, 0.3]

    async def load_vectors(self, namespace, docs):
        out = {}
        for d in docs:
            key = (d.get("namespace", ""), d.get("domain", "core"), d.get("path", ""))
            out[key] = [[0.1, 0.2, 0.3]]
        return out

    def cosine_matrix(self, query_vec, doc_vectors):
        return {key: self.score_by_path.get(key[2], 0.0) for key in doc_vectors}


async def test_search_semantic_falls_back_without_key(graph_service, search_indexer):
    """No embedding key configured -> semantic=True is byte-identical to lexical."""
    await graph_service.create_memory(
        parent_path="",
        content="GraphService owns alias refreshes",
        priority=2,
        title="search_note",
        disclosure="When testing search refresh",
    )

    lexical = await search_indexer.search("GraphService", semantic=False)
    semantic = await search_indexer.search("GraphService", semantic=True)

    assert lexical == semantic
    assert lexical and lexical[0]["uri"] == "core://search_note"


async def test_search_semantic_retrieves_synonym_hit(graph_service, search_indexer, monkeypatch):
    """A query with zero keyword overlap still recalls via the vector pass."""
    await graph_service.create_memory(
        parent_path="",
        content="GraphService owns alias refreshes",
        priority=2,
        title="search_note",
        disclosure="When testing search refresh",
    )
    await graph_service.create_memory(
        parent_path="",
        content="MemoryBrowser renders the rendered search pane",
        priority=2,
        title="browser_note",
        disclosure="When rendering",
    )

    # No keyword overlap with any stored document.
    assert await search_indexer.search("memory engine", semantic=False) == []

    fake = _FakeEmbedder(score_by_path={"search_note": 0.95, "browser_note": 0.1})
    monkeypatch.setattr(search_indexer, "_embedding", fake)

    results = await search_indexer.search("memory engine", semantic=True)

    assert results and results[0]["uri"] == "core://search_note"
    # The lexical path is untouched by the fake.
    assert await search_indexer.search("memory engine", semantic=False) == []


async def test_search_semantic_fuses_and_dedups(graph_service, search_indexer, monkeypatch):
    """Lexical AND hit (A) and vector-only hit (B) both surface, no dupes; A first."""
    await graph_service.create_memory(
        parent_path="",
        content="GraphService owns alias refreshes",
        priority=2,
        title="lexical_hit",
        disclosure="When testing search refresh",
    )
    await graph_service.create_memory(
        parent_path="",
        content="MemoryBrowser renders the rendered search pane",
        priority=2,
        title="semantic_hit",
        disclosure="When rendering",
    )

    fake = _FakeEmbedder(score_by_path={"lexical_hit": 0.9, "semantic_hit": 0.7})
    monkeypatch.setattr(search_indexer, "_embedding", fake)

    results = await search_indexer.search("GraphService", semantic=True)

    uris = [r["uri"] for r in results]
    assert "core://lexical_hit" in uris
    assert "core://semantic_hit" in uris
    assert len(uris) == len(set(uris))  # no duplicate node_uuid
    # AND-pass rank-1 (lexical) outranks a weaker vector-only hit.
    assert results[0]["uri"] == "core://lexical_hit"


async def test_embedding_cache_invalidates_on_update(graph_service, search_indexer, monkeypatch):
    """Content updates change the stored content_hash (stale vectors replaced)."""
    fake = _FakeEmbedder()
    monkeypatch.setattr(search_indexer, "_embedding", fake)

    await graph_service.create_memory(
        parent_path="",
        content="GraphService owns alias refreshes",
        priority=2,
        title="cache_note",
        disclosure="When testing cache",
    )
    # Let the background backfill task run to completion.
    await asyncio.sleep(0.05)

    first = dict(fake.hashes)
    assert first
    assert fake.ensure_calls >= 1

    await graph_service.update_memory("cache_note", content="MemoryBrowser owns rendered search")
    await asyncio.sleep(0.05)

    second = dict(fake.hashes)
    assert second
    # Content changed -> the content hash for every stored segment must change.
    assert second != first
