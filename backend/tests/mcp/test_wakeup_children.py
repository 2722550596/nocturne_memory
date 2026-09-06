"""wakeup / memory-slot boot 槽：子节点不再截断为前 3 个。

此前 _format_memory_clean 用 max_children=3 切片，boot 记忆下多于 3 个
子节点时连 URI 都不显示。改为全量渲染（URI + 想起条件 + snippet）。
"""
import pytest

from system_views import generate_wakeup_view, generate_memory_slot_view


async def _seed_wide_boot(graph_service):
    """一个 boot 节点挂 5 个子节点（超过旧上限 3）。"""
    await graph_service.create_memory(
        parent_path="", content="角色核心设定", priority=1,
        title="persona", domain="core", namespace="",
    )
    for i in range(5):
        await graph_service.create_memory(
            parent_path="persona", content=f"设定细节{i}", priority=5,
            title=f"detail_{i}", domain="core", namespace="",
        )


@pytest.mark.asyncio
async def test_wakeup_shows_all_boot_children(mcp_module, graph_service):
    await _seed_wide_boot(graph_service)

    view = await generate_wakeup_view(["core://persona"], history_limit=0)

    for i in range(5):
        assert f"core://persona/detail_{i}" in view, f"child {i} missing from wakeup view"
    assert "设定细节4" in view  # snippet content present too


@pytest.mark.asyncio
async def test_memory_slot_boot_shows_all_boot_children(mcp_module, graph_service):
    await _seed_wide_boot(graph_service)

    view = await generate_memory_slot_view("boot", ["core://persona"])

    for i in range(5):
        assert f"core://persona/detail_{i}" in view, f"child {i} missing from slot view"
