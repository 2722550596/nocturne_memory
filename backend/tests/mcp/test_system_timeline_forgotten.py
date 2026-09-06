"""system://timeline 与 system://forgotten 视图测试。

- timeline: 按世界时间（world_timestamp）倒序，可带域过滤与 since 日期；
  与 recent（修改时间）不同——修改旧记忆不应改变 timeline 顺序。
- forgotten: 沉睡最久的记忆排最前；browse 过的节点沉睡天数归零；
  从未访问的按 created_at 起算。
"""
import pytest


async def _seed(graph_service):
    # 三个事件，世界时间 3 天 > 1 天 > 10 天前
    await graph_service.create_memory(
        parent_path="", content="最早的事件", priority=5,
        title="oldest", domain="history", namespace="",
        world_timestamp="2020-09-10",
    )
    await graph_service.create_memory(
        parent_path="", content="中间的事件", priority=5,
        title="middle", domain="history", namespace="",
        world_timestamp="2020-09-20",
    )
    await graph_service.create_memory(
        parent_path="", content="最近的事件", priority=5,
        title="newest", domain="history", namespace="",
        world_timestamp="2020-09-28",
    )
    # 一条没世界时间的（不应出现在 timeline）
    await graph_service.create_memory(
        parent_path="", content="无时间记忆", priority=5,
        title="no_time", domain="core", namespace="",
    )


def _msg(result) -> str:
    return result if isinstance(result, str) else getattr(result, "message", str(result))


# ====================================================================
# timeline
# ====================================================================

@pytest.mark.asyncio
async def test_timeline_orders_by_world_time(mcp_module, graph_service):
    """timeline 按世界时间倒序：最新事件在最前。"""
    await _seed(graph_service)

    text = _msg(await mcp_module.browse_memory("system://timeline"))
    assert "世界时间轴" in text
    # 2020-09-28 那条排最前，09-10 排最后
    assert text.index("2020-09-28") < text.index("2020-09-20") < text.index("2020-09-10")
    # 无世界时间的不出现
    assert "无时间记忆" not in text


@pytest.mark.asyncio
async def test_timeline_domain_filter(mcp_module, graph_service):
    """system://timeline/core 只显示 core 域（seed 里 core 没有带时间条目）。"""
    await _seed(graph_service)

    text = _msg(await mcp_module.browse_memory("system://timeline/core"))
    assert "没有带世界时间的记忆" in text


@pytest.mark.asyncio
async def test_timeline_since_date_filter(mcp_module, graph_service):
    """system://timeline/2020-09-15 只保留该日及之后的条目。"""
    await _seed(graph_service)

    text = _msg(await mcp_module.browse_memory("system://timeline/2020-09-15"))
    assert "2020-09-20" in text
    assert "2020-09-28" in text
    assert "2020-09-10" not in text


@pytest.mark.asyncio
async def test_timeline_domain_with_limit(mcp_module, graph_service):
    """system://timeline/history/2 限 history 域、取 2 条。"""
    await _seed(graph_service)

    text = _msg(await mcp_module.browse_memory("system://timeline/history/2"))
    assert "最近的事件" in text
    assert "中间的事件" in text
    assert "最早的事件" not in text


@pytest.mark.asyncio
async def test_forgotten_ranks_by_dormancy(mcp_module, graph_service):
    """沉睡天数最长的排最前；从未访问的按 created_at 起算排最后。"""
    from datetime import datetime, timedelta
    from sqlalchemy import update
    from db.models import Node

    await _seed(graph_service)

    # 直接设定各节点的 last_accessed_at，构造确定的沉睡天数
    async with graph_service.session() as session:
        for name, days in [("oldest", 100), ("middle", 10), ("newest", 1)]:
            mem = await graph_service.get_memory_by_path(
                name, "history", namespace=""
            )
            await session.execute(
                update(Node)
                .where(Node.uuid == mem["node_uuid"])
                .values(
                    last_accessed_at=datetime.now() - timedelta(days=days)
                )
            )
    # no_time 不设 last_accessed_at -> 按 created_at（刚刚）起算，沉睡 0.0 天

    text = _msg(await mcp_module.browse_memory("system://forgotten"))
    assert "沉睡记忆" in text
    # 排序：oldest(100d) -> middle(10d) -> newest(1d) -> no_time(0.0d)
    assert text.index("history://oldest") < text.index("history://middle")
    assert text.index("history://middle") < text.index("history://newest")
    assert text.index("history://newest") < text.index("core://no_time")
    assert "100.0 天没想起" in text
    assert "0.0 天没想起" in text


@pytest.mark.asyncio
async def test_forgotten_browse_resets_dormancy(mcp_module, graph_service):
    """log_access（browse 触发）把沉睡天数归零，节点掉出排行榜头部。"""
    from datetime import datetime, timedelta
    from sqlalchemy import update
    from db.models import Node

    await _seed(graph_service)

    mem = await graph_service.get_memory_by_path("oldest", "history", namespace="")
    async with graph_service.session() as session:
        await session.execute(
            update(Node)
            .where(Node.uuid == mem["node_uuid"])
            .values(last_accessed_at=datetime.now() - timedelta(days=100))
        )

    # 直接 await log_access（browse 里是 create_task 异步发的，测试里同步等）
    await graph_service.log_access(mem["node_uuid"], namespace="", context="test")

    text = _msg(await mcp_module.browse_memory("system://forgotten"))
    assert "0.0 天没想起 | history://oldest" in text


@pytest.mark.asyncio
async def test_forgotten_limit(mcp_module, graph_service):
    """system://forgotten//2 只取 2 条（空域段 + 数字限）。"""
    await _seed(graph_service)

    text = _msg(await mcp_module.browse_memory("system://forgotten//2"))
    # 4 个节点，限 2 -> 只显示 2 条
    uris = [l for l in text.split("\n") if "天没想起" in l]
    assert len(uris) == 2
