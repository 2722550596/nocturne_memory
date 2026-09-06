"""
Parent-path auto-stubbing for the remember tools.

Writing `core://a/b/c` when neither `a` nor `a/b` exists used to raise
"Parent does not exist".  The remember tools now stub the missing ancestors
so the child lands immediately, and the tool result tells the model to
write the real parent content back afterwards.
"""

import pytest


def _message(result):
    return getattr(result, "message", str(result))


@pytest.mark.asyncio
async def test_remember_memory_stubs_missing_parent(mcp_module, graph_service):
    """Missing parent is stubbed instead of erroring; child still lands."""
    result = await mcp_module.remember_memory(
        "core://world/magic/first_flight",
        "她第一次飞起来。",
    )

    assert "已记下记忆" in result
    # The child is really there.
    child = await graph_service.get_memory_by_path("world/magic/first_flight")
    assert child is not None
    assert "第一次飞" in child["content"]

    # The parent got stubbed, not skipped.
    parent = await graph_service.get_memory_by_path("world/magic")
    assert parent is not None
    assert parent["content"] == "（记得补充）"


@pytest.mark.asyncio
async def test_remember_memory_stubs_whole_ancestor_chain(mcp_module, graph_service):
    """Every missing ancestor on the chain is stubbed, shallowest first."""
    await mcp_module.remember_memory("core://x/y/z/leaf", "内容")

    for path in ("x", "x/y", "x/y/z"):
        node = await graph_service.get_memory_by_path(path)
        assert node is not None, path
        assert node["content"] == "（记得补充）", path

    leaf = await graph_service.get_memory_by_path("x/y/z/leaf")
    assert leaf is not None
    assert leaf["content"] == "内容"


@pytest.mark.asyncio
async def test_remember_memory_result_urges_parent_backfill(mcp_module):
    """The result names the stub URIs and asks for a follow-up write."""
    result = await mcp_module.remember_memory("core://alpha/beta/leaf", "内容")
    text = _message(result)

    assert "core://alpha" in text
    assert "core://alpha/beta" in text
    assert "（记得补充）" in text
    assert "edit_memory" in text


@pytest.mark.asyncio
async def test_remember_memory_existing_parent_creates_no_stub(mcp_module, graph_service):
    """An intact ancestor chain stays untouched — no placeholder noise."""
    await graph_service.create_memory(
        parent_path="",
        content="真实的父内容",
        priority=5,
        title="alpha",
        disclosure="",
    )
    result = await mcp_module.remember_memory("core://alpha/leaf", "内容")
    text = _message(result)

    assert "已记下记忆" in text
    assert "（记得补充）" not in text
    assert "edit_memory" not in text

    parent = await graph_service.get_memory_by_path("alpha")
    assert parent["content"] == "真实的父内容"


@pytest.mark.asyncio
async def test_remember_child_memory_stubs_missing_parent(mcp_module, graph_service):
    """remember_child_memory behaves the same way."""
    result = await mcp_module.remember_child_memory(
        parent_uri="core://scenes/teahouse",
        content="他们在茶馆见面。",
        when="当有人提到茶馆",
    )
    text = _message(result)

    assert "core://scenes" in text
    assert "edit_memory" in text

    # parent_uri 本身就是父路径，所以 scenes 与 scenes/teahouse 都被占位，
    # 真正的新记忆挂在 scenes/teahouse 下面。
    for path in ("scenes", "scenes/teahouse"):
        node = await graph_service.get_memory_by_path(path)
        assert node is not None, path
        assert node["content"] == "（记得补充）", path

    child = await graph_service.get_memory_by_path("scenes/teahouse/1")
    assert child is not None
    assert "茶馆" in child["content"]

@pytest.mark.asyncio
async def test_remember_child_memory_root_uri_unchanged(mcp_module):
    """Domain root needs no parent, so nothing is stubbed."""
    result = await mcp_module.remember_child_memory(
        parent_uri="core://",
        content="内容",
        when="随时",
    )
    text = _message(result)

    assert "（记得补充）" not in text


# =============================================================================
# organize_memory / merge_memories
# =============================================================================


@pytest.mark.asyncio
async def test_organize_memory_stubs_missing_topic_parent(mcp_module, graph_service):
    """主题父节点缺失时先补占位，主题照常建立。"""
    await graph_service.create_memory(
        parent_path="", content="碎片一", priority=5, title="frag1", disclosure="",
    )
    await graph_service.create_memory(
        parent_path="", content="碎片二", priority=5, title="frag2", disclosure="",
    )

    result = await mcp_module.organize_memory(
        target_uri="core://topic_a/topic_b/he",
        source_uris=["core://frag1", "core://frag2"],
        content="我对他的整体印象",
    )
    text = _message(result)

    assert "整理好了" in text
    # 占位父链被建出来，且结果里提醒回写
    for path in ("topic_a", "topic_a/topic_b"):
        node = await graph_service.get_memory_by_path(path)
        assert node is not None, path
        assert node["content"] == "（记得补充）", path
    assert "core://topic_a" in text
    assert "edit_memory" in text

    # 主题本身带的是真内容，不是占位
    topic = await graph_service.get_memory_by_path("topic_a/topic_b/he")
    assert topic is not None
    assert topic["content"] == "我对他的整体印象"


@pytest.mark.asyncio
async def test_organize_memory_reports_source_counts(mcp_module, graph_service):
    """关联结果报出「成功/总数」，而不是只报成功数。"""
    await graph_service.create_memory(
        parent_path="", content="碎片一", priority=5, title="frag1", disclosure="",
    )
    await graph_service.create_memory(
        parent_path="", content="碎片二", priority=5, title="frag2", disclosure="",
    )

    result = await mcp_module.organize_memory(
        target_uri="core://topic",
        source_uris=["core://frag1", "core://frag2"],
        content="主题总结",
        mode="link",
    )
    text = _message(result)

    assert "2/2" in text

    # 两条源都真的挂到了主题下
    for name in ("frag1", "frag2"):
        node = await graph_service.get_memory_by_path(f"topic/{name}")
        assert node is not None, name


@pytest.mark.asyncio
async def test_organize_memory_reports_missing_sources(mcp_module, graph_service):
    """源不存在时如实报告，不再静默 pass 假装成功。"""
    await graph_service.create_memory(
        parent_path="", content="碎片一", priority=5, title="frag1", disclosure="",
    )

    result = await mcp_module.organize_memory(
        target_uri="core://topic",
        source_uris=["core://frag1", "core://nope"],
        content="主题总结",
        mode="link",
    )
    text = _message(result)

    assert "1/2" in text
    assert "没关联上的" in text
    assert "nope" in text


@pytest.mark.asyncio
async def test_merge_memories_stubs_missing_parent(mcp_module, graph_service):
    """合并目标父节点缺失时先补占位，结果照常落地。"""
    await graph_service.create_memory(
        parent_path="", content="事件一", priority=5, title="ev1", disclosure="",
    )
    await graph_service.create_memory(
        parent_path="", content="事件二", priority=5, title="ev2", disclosure="",
    )

    result = await mcp_module.merge_memories(
        uris=["core://ev1", "core://ev2"],
        target_uri="core://deep/deeper/tina",
        content="合并后的内容",
    )
    text = _message(result)

    assert "合并完成" in text
    for path in ("deep", "deep/deeper"):
        node = await graph_service.get_memory_by_path(path)
        assert node is not None, path
        assert node["content"] == "（记得补充）", path
    assert "edit_memory" in text

    merged = await graph_service.get_memory_by_path("deep/deeper/tina")
    assert merged is not None
    assert merged["content"] == "合并后的内容"


@pytest.mark.asyncio
async def test_merge_memories_lists_all_missing_sources(mcp_module, graph_service):
    """缺失的源一次列全，而不是撞到第一条就退出。"""
    await graph_service.create_memory(
        parent_path="", content="事件一", priority=5, title="ev1", disclosure="",
    )

    result = await mcp_module.merge_memories(
        uris=["core://ev1", "core://missing_a", "core://missing_b"],
        target_uri="core://ev9",
        content="合并后的内容",
    )
    text = _message(result)

    # 两条缺失都要点名
    assert "core://missing_a" in text
    assert "core://missing_b" in text
    # 有缺失就不该动数据
    assert await graph_service.get_memory_by_path("ev9") is None
    assert (await graph_service.get_memory_by_path("ev1"))["content"] == "事件一"


@pytest.mark.asyncio
async def test_placeholder_parent_is_editable(mcp_module, graph_service):
    """The stub is a normal node, so the promised backfill actually works."""
    await mcp_module.remember_memory("core://gamma/delta", "子内容")

    await mcp_module.edit_memory(
        uri="core://gamma",
        old_text="（记得补充）",
        new_text="伽马是世界的底层规则。",
    )

    parent = await graph_service.get_memory_by_path("gamma")
    assert parent["content"] == "伽马是世界的底层规则。"


# =============================================================================
# system://diagnostic — placeholder write-back list
# =============================================================================


@pytest.mark.asyncio
async def test_diagnostic_lists_placeholder_parents(mcp_module):
    """补出来的占位父节点会出现在诊断报告里。"""
    await mcp_module.remember_memory("core://outer/inner/leaf", "叶子内容")

    view = await mcp_module.browse_memory("system://diagnostic/core")
    text = _message(view)

    assert "Placeholder Parents" in text
    assert "core://outer" in text
    assert "core://outer/inner" in text
    assert "edit_memory" in text


@pytest.mark.asyncio
async def test_diagnostic_reports_child_counts(mcp_module):
    """每条占位报出下面挂了几条记忆，好判断补写的优先级。"""
    await mcp_module.remember_memory("core://hub_a/leaf1", "内容一")
    await mcp_module.remember_memory("core://hub_a/leaf2", "内容二")
    await mcp_module.remember_memory("core://hub_b/leaf3", "内容三")

    text = _message(await mcp_module.browse_memory("system://diagnostic/core"))

    assert "hub_a" in text
    assert "hub_b" in text
    # hub_a 挂了 2 条，hub_b 挂了 1 条；按子数降序，hub_a 应排在前面
    assert text.index("hub_a") < text.index("hub_b")
    assert "2 条记忆挂在下面" in text
    assert "1 条记忆挂在下面" in text


@pytest.mark.asyncio
async def test_diagnostic_drops_placeholder_once_filled(mcp_module, graph_service):
    """回写真实内容之后，该节点从占位清单里消失。"""
    await mcp_module.remember_memory("core://gamma/delta", "子内容")

    before = _message(await mcp_module.browse_memory("system://diagnostic/core"))
    assert "core://gamma" in before

    await mcp_module.edit_memory(
        uri="core://gamma",
        old_text="（记得补充）",
        new_text="伽马是世界的底层规则。",
    )

    after = _message(await mcp_module.browse_memory("system://diagnostic/core"))
    assert "core://gamma" not in after


@pytest.mark.asyncio
async def test_diagnostic_healthy_when_no_placeholders(mcp_module):
    """没有占位时报告依旧干净，不会凭空多出一节。"""
    await mcp_module.remember_memory("core://plain", "普通内容")

    text = _message(await mcp_module.browse_memory("system://diagnostic/core"))

    assert "Placeholder Parents" not in text
    assert "（记得补充）" not in text
