"""
Tests for archive_history — the scene-recording tool.

Covers the GM-material workflow: history/raw can be file paths (WSL absolute
or Windows UNC via \\wsl.localhost\...) instead of hand-written content, with
title inferred from the file name.
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


def test_resolve_content_or_path_wsl_absolute(mcp_module, tmp_path):
    f = tmp_path / "sunday_note_0927.md"
    f.write_text("## 场景概览\n周日傍晚，独自在书房。", encoding="utf-8")
    assert mcp_module._resolve_content_or_path(str(f)) == "## 场景概览\n周日傍晚，独自在书房。"
    # 非路径按字面内容返回
    assert mcp_module._resolve_content_or_path("直接写的内容") == "直接写的内容"
    assert mcp_module._resolve_content_or_path(None) == ""


def test_resolve_content_or_path_unc_conversion(mcp_module):
    # \\wsl.localhost\Ubuntu\home\yoshix7ti\... → /home/yoshix7ti/...
    with pytest.raises(ValueError) as exc:
        mcp_module._resolve_content_or_path(
            r"\\wsl.localhost\Ubuntu\home\yoshix7ti\world\export\sunday_note_0927.md"
        )
    assert "/home/yoshix7ti/world/export/sunday_note_0927.md" in str(exc.value)


def test_infer_title_from_path(mcp_module):
    f = mcp_module._infer_title_from_path
    assert f("/home/u/export/history/elias/sunday_note_0927.md") == "sunday_note_0927"
    assert f(r"\\wsl.localhost\Ubuntu\home\u\export\sunday_note_0927.md") == "sunday_note_0927"
    assert f("plain_content_without_path") == "plain_content_without_path"


@pytest.mark.asyncio
async def test_archive_history_content_mode(mcp_module, graph_service):
    """回归：直接传内容的老用法不受影响。"""
    res = await mcp_module.archive_history(title="tavern_brawl", history="酒馆里打了一架。")
    assert "history://scenes/tavern_brawl" in getattr(res, "message", str(res))
    mem = await graph_service.get_memory_by_path("scenes/tavern_brawl", "history")
    assert mem is not None
    assert mem["content"] == "酒馆里打了一架。"


@pytest.mark.asyncio
async def test_archive_history_raw_content(mcp_module, graph_service):
    res = await mcp_module.archive_history(
        title="meet_tina", history="见到了蒂娜。", raw="完整的原始对话……"
    )
    assert "history://scenes/meet_tina" in getattr(res, "message", str(res))
    raw = await graph_service.get_memory_by_path("scenes/meet_tina_raw", "history_raw")
    assert raw is not None
    assert raw["content"] == "完整的原始对话……"


@pytest.mark.asyncio
async def test_archive_history_from_file_with_inferred_title(mcp_module, graph_service, tmp_path):
    """GM 素材文件直接投喂：路径读取 + title 自动从文件名推断。"""
    note = tmp_path / "sunday_note_0927.md"
    note.write_text("## 场景概览\n周日傍晚，天光将暗未暗。\n## 我的状态\n独处。", encoding="utf-8")
    raw_file = tmp_path / "raw.md"
    raw_file.write_text("完整的原始对话记录……", encoding="utf-8")

    res = await mcp_module.archive_history(history=str(note), raw=str(raw_file))
    assert "history://scenes/sunday_note_0927" in getattr(res, "message", str(res))

    mem = await graph_service.get_memory_by_path("scenes/sunday_note_0927", "history")
    assert mem is not None
    assert mem["content"] == "## 场景概览\n周日傍晚，天光将暗未暗。\n## 我的状态\n独处。"
    raw = await graph_service.get_memory_by_path("scenes/sunday_note_0927_raw", "history_raw")
    assert raw is not None
    assert raw["content"] == "完整的原始对话记录……"


@pytest.mark.asyncio
async def test_archive_history_unc_path_rejected_clearly(mcp_module):
    """UNC 路径转换成 WSL 路径后读不到文件时报错要带转换后的路径。"""
    res = await mcp_module.archive_history(
        history=r"\\wsl.localhost\Ubuntu\home\yoshix7ti\world\export\missing_0927.md"
    )
    msg = res if isinstance(res, str) else getattr(res, "message", str(res))
    assert "存档失败" in msg
    assert "/home/yoshix7ti/world/export/missing_0927.md" in msg


@pytest.mark.asyncio
async def test_archive_history_empty_history(mcp_module):
    res = await mcp_module.archive_history(title="x", history="")
    msg = getattr(res, "message", str(res))
    assert "history 不能为空" in msg


@pytest.mark.asyncio
async def test_archive_history_bad_title(mcp_module):
    res = await mcp_module.archive_history(title="坏标题!", history="内容")
    msg = getattr(res, "message", str(res))
    assert "title 必须提供" in msg


@pytest.mark.asyncio
async def test_archive_history_character_scope(mcp_module, graph_service):
    """character_id 走 namespace_scope：记到指定角色的 namespace 下。"""
    res = await mcp_module.archive_history(
        title="mingrui_scene", history="明瑞的场景。", character_id="mingrui"
    )
    assert "history://scenes/mingrui_scene" in getattr(res, "message", str(res))
    # 默认 namespace 里没有，mingrui namespace 里有
    assert await graph_service.get_memory_by_path("scenes/mingrui_scene", "history") is None
    mem = await graph_service.get_memory_by_path(
        "scenes/mingrui_scene", "history", namespace="mingrui"
    )
    assert mem is not None
