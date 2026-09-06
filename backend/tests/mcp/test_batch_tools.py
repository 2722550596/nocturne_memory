"""
Tests for the batch maintenance tools:
  rename_memory / move_memory / batch_move_memories /
  batch_forget_memories / batch_edit_memories

These cover the memory-hygiene workflows (distillation, cleanup, re-grading)
that motivated the batch surface: cross-domain moves with auto-created target
parents, subtree-following renames, dry-run previews that never touch the DB,
nested-delete coverage, and per-item failure isolation.
"""

import pytest


async def _seed(graph_service, domain="core", parent="", title=None, content="x",
                priority=5, disclosure="when testing"):
    return await graph_service.create_memory(
        parent_path=parent,
        content=content,
        priority=priority,
        title=title,
        disclosure=disclosure,
        domain=domain,
    )


# =============================================================================
# rename_memory
# =============================================================================


@pytest.mark.asyncio
async def test_rename_memory_moves_subtree(mcp_module, graph_service):
    """Rename re-paths the node AND its descendants; old paths disappear."""
    await _seed(graph_service, title="topic", content="主题内容")
    await _seed(graph_service, parent="topic", title="child", content="子节点内容")

    res = await mcp_module.rename_memory("core://topic", "renamed_topic")
    assert "core://renamed_topic" in getattr(res, "message", str(res))

    moved = await graph_service.get_memory_by_path("renamed_topic", "core")
    assert moved is not None
    assert moved["content"] == "主题内容"
    assert await graph_service.get_memory_by_path("topic", "core") is None

    child = await graph_service.get_memory_by_path("renamed_topic/child", "core")
    assert child is not None
    assert child["content"] == "子节点内容"
    assert await graph_service.get_memory_by_path("topic/child", "core") is None


@pytest.mark.asyncio
async def test_rename_memory_rejects_bad_title(mcp_module, graph_service):
    await _seed(graph_service, title="topic")

    res = await mcp_module.rename_memory("core://topic", "bad title/with slash")
    assert "只能包含字母" in getattr(res, "message", str(res))
    # untouched
    assert (await graph_service.get_memory_by_path("topic", "core")) is not None


# =============================================================================
# move_memory
# =============================================================================


@pytest.mark.asyncio
async def test_move_memory_across_domains_creates_parent(mcp_module, graph_service):
    """Cross-domain move auto-creates the missing target directory container."""
    await _seed(graph_service, title="scene_a", content="场景内容")

    res = await mcp_module.move_memory("core://scene_a", "notes://archive/scene_a")
    assert "notes://archive/scene_a" in getattr(res, "message", str(res))

    moved = await graph_service.get_memory_by_path("archive/scene_a", "notes")
    assert moved is not None
    assert moved["content"] == "场景内容"
    assert await graph_service.get_memory_by_path("scene_a", "core") is None
    # the target parent container was created automatically
    parent = await graph_service.get_memory_by_path("archive", "notes")
    assert parent is not None


@pytest.mark.asyncio
async def test_move_memory_target_conflict_rejected(mcp_module, graph_service):
    await _seed(graph_service, title="src", content="源")
    await _seed(graph_service, title="dst", content="目标")

    res = await mcp_module.move_memory("core://src", "core://dst")
    assert "已经存在" in getattr(res, "message", str(res))
    # source stays put
    assert (await graph_service.get_memory_by_path("src", "core")) is not None
    assert (await graph_service.get_memory_by_path("dst", "core")) is not None


@pytest.mark.asyncio
async def test_move_memory_missing_source_rejected(mcp_module):
    res = await mcp_module.move_memory("core://nope", "notes://nope")
    assert "没找到源" in getattr(res, "message", str(res))


# =============================================================================
# batch_move_memories
# =============================================================================


@pytest.mark.asyncio
async def test_batch_move_memories_executes_all(mcp_module, graph_service):
    for i in range(3):
        await _seed(graph_service, title=f"raw_{i}", content=f"内容{i}")

    res = await mcp_module.batch_move_memories([
        {"source_uri": f"core://raw_{i}", "target_uri": f"notes://scenes/raw_{i}"}
        for i in range(3)
    ])
    msg = getattr(res, "message", str(res))
    assert "成功 3 条" in msg
    assert "失败 0 条" in msg
    for i in range(3):
        assert (await graph_service.get_memory_by_path(f"scenes/raw_{i}", "notes")) is not None
        assert (await graph_service.get_memory_by_path(f"raw_{i}", "core")) is None


@pytest.mark.asyncio
async def test_batch_move_dry_run_does_not_touch(mcp_module, graph_service):
    await _seed(graph_service, title="keep_me")

    res = await mcp_module.batch_move_memories(
        [{"source_uri": "core://keep_me", "target_uri": "notes://keep_me"}],
        dry_run=True,
    )
    msg = getattr(res, "message", str(res))
    assert "预览" in msg
    assert "将变为 notes://keep_me" in msg
    # nothing landed
    assert (await graph_service.get_memory_by_path("keep_me", "core")) is not None
    assert (await graph_service.get_memory_by_path("keep_me", "notes")) is None


@pytest.mark.asyncio
async def test_batch_move_partial_failure_isolated(mcp_module, graph_service):
    await _seed(graph_service, title="good")

    res = await mcp_module.batch_move_memories([
        {"source_uri": "core://good", "target_uri": "notes://good"},
        {"source_uri": "core://missing", "target_uri": "notes://missing"},
    ])
    msg = getattr(res, "message", str(res))
    assert "成功 1 条" in msg
    assert "失败 1 条" in msg
    assert "没找到源" in msg
    # the good one still moved
    assert (await graph_service.get_memory_by_path("good", "notes")) is not None


# =============================================================================
# batch_forget_memories
# =============================================================================


@pytest.mark.asyncio
async def test_batch_forget_nested_deletes_all(mcp_module, graph_service):
    """Out-of-order nested deletes succeed: leaves go first (depth ordering)."""
    await _seed(graph_service, title="parent", content="父")
    await _seed(graph_service, parent="parent", title="child", content="子")
    await _seed(graph_service, title="solo", content="独立")

    res = await mcp_module.batch_forget_memories(
        ["core://parent", "core://parent/child", "core://solo"]
    )
    msg = getattr(res, "message", str(res))
    assert "成功 3 条" in msg
    assert "失败 0 条" in msg
    assert (await graph_service.get_memory_by_path("parent", "core")) is None
    assert (await graph_service.get_memory_by_path("parent/child", "core")) is None
    assert (await graph_service.get_memory_by_path("solo", "core")) is None


@pytest.mark.asyncio
async def test_batch_forget_duplicate_skipped(mcp_module, graph_service):
    """A URI repeated in the batch is skipped after its first deletion."""
    await _seed(graph_service, title="dup", content="重复目标")

    res = await mcp_module.batch_forget_memories(["core://dup", "core://dup"])
    msg = getattr(res, "message", str(res))
    assert "成功 1 条" in msg
    assert "跳过 1 条" in msg
    assert (await graph_service.get_memory_by_path("dup", "core")) is None


@pytest.mark.asyncio
async def test_batch_forget_dry_run_preserves(mcp_module, graph_service):
    await _seed(graph_service, title="victim")

    res = await mcp_module.batch_forget_memories(["core://victim"], dry_run=True)
    msg = getattr(res, "message", str(res))
    assert "预览" in msg
    assert "将删除" in msg
    assert (await graph_service.get_memory_by_path("victim", "core")) is not None


@pytest.mark.asyncio
async def test_batch_forget_missing_reported(mcp_module):
    res = await mcp_module.batch_forget_memories(["core://ghost"])
    msg = getattr(res, "message", str(res))
    assert "失败 1 条" in msg
    assert "没找到" in msg


# =============================================================================
# batch_edit_memories
# =============================================================================


@pytest.mark.asyncio
async def test_batch_edit_importance_and_append(mcp_module, graph_service):
    await _seed(graph_service, title="a", content="甲", priority=5)
    await _seed(graph_service, title="b", content="乙", priority=5)

    res = await mcp_module.batch_edit_memories(
        ["core://a", "core://b"], importance=2, append="\n（维护注记）"
    )
    msg = getattr(res, "message", str(res))
    assert "成功 2 条" in msg
    for name in ("a", "b"):
        mem = await graph_service.get_memory_by_path(name, "core")
        assert mem["priority"] == 2
        assert mem["content"].endswith("（维护注记）")


@pytest.mark.asyncio
async def test_batch_edit_dry_run_does_not_change(mcp_module, graph_service):
    await _seed(graph_service, title="a", content="甲", priority=5)

    res = await mcp_module.batch_edit_memories(["core://a"], importance=1, dry_run=True)
    msg = getattr(res, "message", str(res))
    assert "预览" in msg
    assert "重要性 5 → 1" in msg
    mem = await graph_service.get_memory_by_path("a", "core")
    assert mem["priority"] == 5


@pytest.mark.asyncio
async def test_batch_edit_requires_field(mcp_module):
    res = await mcp_module.batch_edit_memories(["core://a"])
    assert "至少要提供一种修改" in getattr(res, "message", str(res))


@pytest.mark.asyncio
async def test_batch_edit_missing_reported(mcp_module):
    res = await mcp_module.batch_edit_memories(["core://ghost"], importance=1)
    msg = getattr(res, "message", str(res))
    assert "失败 1 条" in msg
