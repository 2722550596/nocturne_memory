"""
End-to-end integration tests: the character-oriented RP MCP tools with
namespace isolation.

Test matrix:
  1. Full CRUD flow with two agents (browse/remember_child/edit/forget/link/tag/search)
  2. system://boot isolation
  3. system://index and system://index/<domain> isolation
  4. system://recent isolation
  5. system://glossary cross-check (agent_b must not see agent_a's triggers)
  6. search_memory with domain filter + namespace
  7. Delete cascade (parent delete in ns_a doesn't affect ns_b)
  8. Backward compatibility (default namespace full flow)

Pinned to the RP-tool rewrite (commit 02f610a): browse_memory, search_memory,
remember_child_memory, edit_memory, forget_memory, link_memory, tag_memory.
"""

import os
import sys
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


@pytest_asyncio.fixture
async def mcp_env(tmp_path, monkeypatch):
    """Set up a fresh in-memory DB backed by a real config file, and patch the
    global service singletons (including the preset service) so namespace-aware
    boot URIs resolve against this isolated database."""
    import json
    import config

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("SNAPSHOT_DIR", str(snapshot_dir))
    monkeypatch.setenv("API_TOKEN", "")

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "database_url": db_url,
        "valid_domains": ["core", "writer", "game", "notes", "project", "system"],
        "boot_uris": {"": ["core://agent", "core://my_user"]},
        "host": "127.0.0.1",
        "web_port": 8233,
        "auto_open_browser": False,
        "api_token": None,
        "locale": "en",
    }))
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    config._invalidate()

    import db as db_pkg
    from db.database import DatabaseManager
    from db.search import SearchIndexer
    from db.glossary import GlossaryService
    from db.graph import GraphService
    from db.presets import PresetService

    db = DatabaseManager(db_url)
    await db.init_db()
    search = SearchIndexer(db)
    glossary = GlossaryService(db, search)
    graph = GraphService(db, search)
    preset = PresetService(db)

    old = (
        db_pkg._db_manager, db_pkg._graph_service, db_pkg._search_indexer,
        db_pkg._glossary_service, db_pkg._preset_service,
    )
    db_pkg._db_manager = db
    db_pkg._graph_service = graph
    db_pkg._search_indexer = search
    db_pkg._glossary_service = glossary
    db_pkg._preset_service = preset

    import db.snapshot as snapshot_module
    snapshot_module._store = None

    import mcp_server as mcp_module
    import importlib
    importlib.reload(mcp_module)

    yield {"preset": preset}

    db_pkg._db_manager, db_pkg._graph_service, db_pkg._search_indexer, \
        db_pkg._glossary_service, db_pkg._preset_service = old
    snapshot_module._store = None
    await db.close()


# ====================================================================
# Helper: seed both agents
# ====================================================================

async def _seed_two_agents():
    """Create standard memories for agent_a and agent_b."""
    from mcp_server import remember_child_memory, tag_memory
    from db.namespace import set_namespace

    set_namespace("agent_a")
    await remember_child_memory(
        "core://", "I am Agent A's identity.", importance=0,
        when="When asking who I am", title="agent",
    )
    await remember_child_memory(
        "core://", "Agent A met User on 2026-01-01.", importance=1,
        when="When talking about my user", title="my_user",
    )
    await remember_child_memory(
        "core://", "Agent A writer notes.", importance=2,
        when="When discussing writing", title="notes",
    )
    await tag_memory("core://agent", add=["soul_trigger_a"])

    set_namespace("agent_b")
    await remember_child_memory(
        "core://", "I am Agent B's identity.", importance=0,
        when="When asking who I am", title="agent",
    )
    await tag_memory("core://agent", add=["soul_trigger_b"])


# ====================================================================
# 1. Full CRUD + alias + triggers + search
# ====================================================================

@pytest.mark.asyncio
async def test_full_mcp_crud_with_namespace_isolation(mcp_env):
    """All RP MCP tools work correctly; two agents are fully isolated."""
    from mcp_server import (
        browse_memory, remember_child_memory, edit_memory,
        forget_memory, link_memory, tag_memory, search_memory,
    )
    from db.namespace import set_namespace

    await _seed_two_agents()

    # --- Read isolation ---
    set_namespace("agent_a")
    assert "Agent A" in await browse_memory("core://agent")
    assert "Agent B" not in await browse_memory("core://agent")

    set_namespace("agent_b")
    assert "Agent B" in await browse_memory("core://agent")
    assert "Agent A" not in await browse_memory("core://agent")

    # --- Update isolation ---
    set_namespace("agent_a")
    result = await edit_memory(
        "core://agent",
        old_text="I am Agent A's identity.",
        new_text="I am Agent A's evolved identity.",
    )
    assert "改好" in result
    assert "evolved" in await browse_memory("core://agent")

    set_namespace("agent_b")
    assert "evolved" not in await browse_memory("core://agent")

    # --- Alias isolation ---
    set_namespace("agent_a")
    link_result = await link_memory(
        target_uri="core://agent",
        new_uri="writer://agent_copy",
        importance=5,
        when="When mirroring agent identity",
    )
    assert "writer://agent_copy" in link_result

    set_namespace("agent_b")
    alias_b = await browse_memory("writer://agent_copy")
    assert "not found" in alias_b or "出错了" in alias_b

    # --- Delete isolation ---
    set_namespace("agent_a")
    forget_result = await forget_memory("core://my_user")
    assert "忘掉" in forget_result
    deleted = await browse_memory("core://my_user")
    assert "not found" in deleted or "出错了" in deleted

    set_namespace("agent_b")
    assert "Agent B" in await browse_memory("core://agent")


# ====================================================================
# 2. system://boot isolation
# ====================================================================

@pytest.mark.asyncio
async def test_system_boot_isolation(mcp_env):
    """system://boot loads core memories from the current namespace only."""
    from mcp_server import browse_memory
    from db.namespace import set_namespace

    await _seed_two_agents()

    set_namespace("agent_a")
    boot_a = await browse_memory("system://boot")
    assert "Agent A" in boot_a
    assert "Agent B" not in boot_a

    set_namespace("agent_b")
    boot_b = await browse_memory("system://boot")
    assert "Agent B" in boot_b
    assert "Agent A" not in boot_b


@pytest.mark.asyncio
async def test_system_boot_per_namespace_uris(mcp_env):
    """Per-namespace boot URI overrides narrow the loaded set."""
    from mcp_server import browse_memory
    from db.namespace import set_namespace

    await _seed_two_agents()

    preset = mcp_env["preset"]
    # agent_a and agent_b boot only core://agent (skip core://my_user).
    await preset.set_boot_uris("agent_a", ["core://agent"])
    await preset.set_boot_uris("agent_b", ["core://agent"])

    set_namespace("agent_a")
    boot_a = await browse_memory("system://boot")
    assert "Agent A" in boot_a
    # core://my_user is in the global fallback but NOT in agent_a's override.
    assert "met User" not in boot_a

    set_namespace("agent_b")
    boot_b = await browse_memory("system://boot")
    assert "Agent B" in boot_b
    assert "Agent A" not in boot_b

    await preset.delete_boot_uris("agent_a")
    await preset.delete_boot_uris("agent_b")


@pytest.mark.asyncio
async def test_system_boot_per_namespace_empty_override(mcp_env):
    """An explicit empty boot-URI list for a namespace must NOT fall back to
    the global list."""
    from mcp_server import browse_memory
    from db.namespace import set_namespace

    # Seed something under agent_c so the namespace exists.
    set_namespace("agent_c")
    from mcp_server import remember_child_memory
    await remember_child_memory(
        "core://", "Agent C identity.", importance=0,
        when="When asking who I am", title="agent",
    )

    preset = mcp_env["preset"]
    # Global fallback lists core://global; agent_c gets an explicit empty list.
    await preset.set_boot_uris("", ["core://agent"])
    await preset.set_boot_uris("agent_c", [])

    try:
        set_namespace("agent_c")
        boot_c = await browse_memory("system://boot")
        # Should NOT load anything (0/0), and must not fall back to global.
        assert "0/0" in boot_c
        assert "Agent C" not in boot_c
    finally:
        await preset.delete_boot_uris("agent_c")
        await preset.set_boot_uris("", ["core://agent", "core://my_user"])


# ====================================================================
# 3. system://index and system://index/<domain>
# ====================================================================

@pytest.mark.asyncio
async def test_system_index_isolation(mcp_env):
    """system://index shows only the current namespace's memory tree."""
    from mcp_server import browse_memory
    from db.namespace import set_namespace

    await _seed_two_agents()

    set_namespace("agent_a")
    index_a = await browse_memory("system://index/core")
    assert "my_user" in index_a
    assert "notes" in index_a

    set_namespace("agent_b")
    index_b = await browse_memory("system://index/core")
    assert "my_user" not in index_b
    assert "notes" not in index_b


@pytest.mark.asyncio
async def test_system_index_domain_isolation(mcp_env):
    """system://index/<domain> only shows paths in the requested domain within the namespace."""
    from mcp_server import browse_memory, remember_child_memory
    from db.namespace import set_namespace

    set_namespace("agent_a")
    await remember_child_memory(
        "core://", "A core data", importance=0,
        when="When reviewing agent A core index", title="a_core",
    )
    await remember_child_memory(
        "writer://", "A writer data", importance=0,
        when="When reviewing agent A writer index", title="a_writer",
    )

    set_namespace("agent_b")
    await remember_child_memory(
        "core://", "B core data", importance=0,
        when="When reviewing agent B core index", title="b_core",
    )

    set_namespace("agent_a")
    index_core = await browse_memory("system://index/core")
    assert "a_core" in index_core
    assert "a_writer" not in index_core
    assert "b_core" not in index_core

    set_namespace("agent_b")
    index_core_b = await browse_memory("system://index/core")
    assert "b_core" in index_core_b
    assert "a_core" not in index_core_b


# ====================================================================
# 4. system://recent isolation
# ====================================================================

@pytest.mark.asyncio
async def test_system_recent_isolation(mcp_env):
    """system://recent shows only the current namespace's recent memories."""
    from mcp_server import browse_memory
    from db.namespace import set_namespace

    await _seed_two_agents()

    set_namespace("agent_a")
    recent_a = await browse_memory("system://recent")
    assert "core://agent" in recent_a
    assert "my_user" in recent_a

    set_namespace("agent_b")
    recent_b = await browse_memory("system://recent")
    assert "my_user" not in recent_b


# ====================================================================
# 5. system://glossary cross-check
# ====================================================================

@pytest.mark.asyncio
async def test_system_glossary_isolation(mcp_env):
    """Each agent's glossary only contains its own triggers, not the other's."""
    from mcp_server import browse_memory
    from db.namespace import set_namespace

    await _seed_two_agents()

    set_namespace("agent_a")
    glossary_a = await browse_memory("system://glossary")
    assert "soul_trigger_a" in glossary_a
    assert "soul_trigger_b" not in glossary_a

    set_namespace("agent_b")
    glossary_b = await browse_memory("system://glossary")
    assert "soul_trigger_b" in glossary_b
    assert "soul_trigger_a" not in glossary_b


# ====================================================================
# 6. search_memory with domain filter + namespace
# ====================================================================

@pytest.mark.asyncio
async def test_search_memory_domain_filter_isolation(mcp_env):
    """search_memory(domain=...) respects both domain and namespace."""
    from mcp_server import search_memory
    from db.namespace import set_namespace

    await _seed_two_agents()

    set_namespace("agent_a")
    result = await search_memory("identity", domain="core")
    assert "Agent A" in result
    assert "Agent B" not in result

    # agent_a has no writer:// content matching "identity"
    result_writer = await search_memory("identity", domain="writer")
    assert "Agent A" not in result_writer

    set_namespace("agent_b")
    result_b = await search_memory("identity", domain="core")
    assert "Agent B" in result_b
    assert "Agent A" not in result_b


# ====================================================================
# 7. Delete cascade isolation
# ====================================================================

@pytest.mark.asyncio
async def test_delete_cascade_isolation(mcp_env):
    """Deleting a parent in agent_a does not affect agent_b's subtree."""
    from mcp_server import remember_child_memory, forget_memory, browse_memory
    from db.namespace import set_namespace

    for ns in ("agent_a", "agent_b"):
        set_namespace(ns)
        await remember_child_memory(
            "core://", f"Parent {ns}", importance=0, title="parent",
            when=f"When reviewing parent memory in {ns}",
        )
        await remember_child_memory(
            "core://parent", f"Child {ns}", importance=1, title="child",
            when=f"When reviewing child memory in {ns}",
        )

    set_namespace("agent_a")
    await forget_memory("core://parent/child")
    await forget_memory("core://parent")

    deleted_parent = await browse_memory("core://parent")
    assert "not found" in deleted_parent or "出错了" in deleted_parent

    set_namespace("agent_b")
    assert "Parent agent_b" in await browse_memory("core://parent")
    assert "Child agent_b" in await browse_memory("core://parent/child")


# ====================================================================
# 8. Backward compatibility - default namespace
# ====================================================================

@pytest.mark.asyncio
async def test_default_namespace_mcp_full_flow(mcp_env):
    """When no namespace is configured (empty string), all MCP tools work as before."""
    from mcp_server import (
        browse_memory, remember_child_memory, edit_memory,
        forget_memory, link_memory, tag_memory, search_memory,
    )
    from db.namespace import set_namespace

    set_namespace("")

    # Create
    create_agent = await remember_child_memory(
        "core://", "Default agent identity.", importance=0,
        title="agent", when="When asking who I am",
    )
    assert "core://agent" in create_agent
    create_user = await remember_child_memory(
        "core://", "Default user info.", importance=1,
        title="my_user", when="When talking about user",
    )
    assert "core://my_user" in create_user

    # Read
    content = await browse_memory("core://agent")
    assert "Default agent identity" in content

    # Boot
    boot = await browse_memory("system://boot")
    assert "Default agent identity" in boot

    # Index
    index = await browse_memory("system://index/core")
    assert "agent" in index
    assert "my_user" in index

    # Recent
    recent = await browse_memory("system://recent")
    assert "core://agent" in recent

    # Update
    update_result = await edit_memory(
        "core://agent", old_text="Default agent identity.",
        new_text="Updated default agent identity.",
    )
    assert "改好" in update_result

    # Search
    results = await search_memory("identity")
    assert "Updated default agent" in results

    # Alias
    link_result = await link_memory(
        target_uri="core://agent",
        new_uri="writer://agent_ref",
        importance=5,
        when="When mirroring default agent identity",
    )
    assert "writer://agent_ref" in link_result

    alias_content = await browse_memory("writer://agent_ref")
    assert "Updated default agent" in alias_content

    # Triggers
    trigger_result = await tag_memory("core://agent", add=["default_trigger"])
    assert "default_trigger" in trigger_result
    glossary = await browse_memory("system://glossary")
    assert "default_trigger" in glossary

    # Delete
    forget_result = await forget_memory("core://my_user")
    assert "忘掉" in forget_result
    deleted = await browse_memory("core://my_user")
    assert "not found" in deleted or "出错了" in deleted

    # Parent still alive
    assert "Updated default agent" in await browse_memory("core://agent")
