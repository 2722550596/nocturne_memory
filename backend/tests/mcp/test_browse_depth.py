"""browse_memory depth / subtree expansion tests.

Covers the depth= parameter added to browse_memory:
- depth=0 keeps the original "this node + direct-child URI list" behaviour.
- depth=1 expands direct children's full content inline.
- depth=N / -1 recurses the subtree.
- max_nodes caps rendered node bodies and notes the omission.
"""
import pytest


async def _seed_tree(graph_service):
    """Build: root -> topic -> child -> grandchild, plus a sibling of topic."""
    await graph_service.create_memory(
        parent_path="", content="根节点内容", priority=5,
        title="root", domain="core", namespace="",
    )
    await graph_service.create_memory(
        parent_path="root", content="主题节点内容", priority=5,
        title="topic", domain="core", namespace="",
    )
    await graph_service.create_memory(
        parent_path="root", content="兄弟节点内容", priority=5,
        title="sibling", domain="core", namespace="",
    )
    await graph_service.create_memory(
        parent_path="root/topic", content="子节点内容", priority=5,
        title="child", domain="core", namespace="",
    )
    await graph_service.create_memory(
        parent_path="root/topic/child", content="孙节点内容", priority=5,
        title="grandchild", domain="core", namespace="",
    )


@pytest.mark.asyncio
async def test_depth_zero_lists_child_uris_only(mcp_module, graph_service):
    """Default behaviour: node content + direct-child URI list, no child bodies."""
    await _seed_tree(graph_service)

    out = await mcp_module.browse_memory("core://root", depth=0)
    text = out if isinstance(out, str) else getattr(out, "message", str(out))

    assert "根节点内容" in text
    assert "core://root/topic" in text
    assert "core://root/sibling" in text
    # child body must NOT be inlined at depth 0
    assert "主题节点内容" not in text
    assert "兄弟节点内容" not in text


@pytest.mark.asyncio
async def test_depth_one_expands_direct_children(mcp_module, graph_service):
    """depth=1: this node + direct children's full content, grandchild not inlined."""
    await _seed_tree(graph_service)

    out = await mcp_module.browse_memory("core://root", depth=1)
    text = out if isinstance(out, str) else getattr(out, "message", str(out))

    assert "根节点内容" in text
    assert "主题节点内容" in text
    assert "兄弟节点内容" in text
    # grandchild is two levels down -> not shown at depth=1
    assert "子节点内容" not in text
    assert "孙节点内容" not in text


@pytest.mark.asyncio
async def test_depth_minus_one_full_subtree(mcp_module, graph_service):
    """depth=-1: the entire subtree is expanded, all levels present."""
    await _seed_tree(graph_service)

    out = await mcp_module.browse_memory("core://root", depth=-1)
    text = out if isinstance(out, str) else getattr(out, "message", str(out))

    for expected in ("根节点内容", "主题节点内容", "兄弟节点内容",
                     "子节点内容", "孙节点内容"):
        assert expected in text, f"missing '{expected}' in subtree view"
    # every node appears as a subtree block heading
    assert text.count("■") >= 4


@pytest.mark.asyncio
async def test_max_nodes_truncates_subtree(mcp_module, graph_service):
    """max_nodes limits how many node bodies render; remainder are noted."""
    await _seed_tree(graph_service)

    out = await mcp_module.browse_memory("core://root", depth=-1, max_nodes=2)
    text = out if isinstance(out, str) else getattr(out, "message", str(out))

    # root (1) + one child (2) consume the budget; further bodies are omitted
    assert "内容省略：已达 max_nodes 上限" in text
    # at least one deep body is NOT rendered
    assert "孙节点内容" not in text


@pytest.mark.asyncio
async def test_default_depth_unchanged(mcp_module, graph_service):
    """Omitting depth behaves exactly like depth=0 (backwards compatible)."""
    await _seed_tree(graph_service)

    explicit = await mcp_module.browse_memory("core://root", depth=0)
    default = await mcp_module.browse_memory("core://root")
    e = explicit if isinstance(explicit, str) else getattr(explicit, "message", str(explicit))
    d = default if isinstance(default, str) else getattr(default, "message", str(default))

    assert e == d
