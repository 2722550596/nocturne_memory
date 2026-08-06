import pytest
import hashlib
import json
from httpx import ASGITransport, AsyncClient

from db.snapshot import get_changeset_store, commit_checkpoint
from main import app

pytestmark = pytest.mark.asyncio

async def test_checkpoint_empty_pool_returns_none(graph_service, tmp_path, monkeypatch):
    store = get_changeset_store()
    
    # Checkpoint when pool is empty
    async with graph_service.session() as session:
        rev_id = await commit_checkpoint(session)
    assert rev_id is None

async def test_checkpoint_creates_revision_advances_head(graph_service):
    store = get_changeset_store()
    
    # 1. Create a dummy change via graph_service
    # Manually populate the pool as `_record_rows` would
    before_state = {}
    after_state = {"nodes": [{"uuid": "test_node"}]}
    store.record_many(before_state, after_state)
    
    assert store.get_change_count() > 0
    old_head = store.get_head_revision_id()
    
    # 2. Commit checkpoint
    async with graph_service.session() as session:
        rev_id = await commit_checkpoint(session)
    assert rev_id is not None
    assert rev_id != old_head
    
    # 3. Verify HEAD advanced
    new_head = store.get_head_revision_id()
    assert new_head == rev_id
    
    # 4. Verify pool NOT cleared
    assert store.get_change_count() > 0
    rows = store.load_all_changed_rows()
    assert "nodes:test_node" in rows
    
async def test_checkpoint_dedupes_unchanged_pool(graph_service):
    store = get_changeset_store()
    before_state = {}
    after_state = {"nodes": [{"uuid": "test_node_2"}]}
    store.record_many(before_state, after_state)
    
    # First checkpoint
    async with graph_service.session() as session:
        rev_id1 = await commit_checkpoint(session)
    assert rev_id1 is not None
    
    # Second checkpoint immediately (pool unchanged)
    async with graph_service.session() as session:
        rev_id2 = await commit_checkpoint(session)
    assert rev_id2 is None
    
    after_state2 = {"nodes": [{"uuid": "test_node_3"}]}
    store.record_many(before_state, after_state2)

    
    async with graph_service.session() as session:
        rev_id3 = await commit_checkpoint(session)
    assert rev_id3 is not None
    assert rev_id3 != rev_id1
