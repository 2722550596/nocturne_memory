# pyright: reportMissingImports=false

"""
MCP Server for Nocturne Memory System (SQLite Backend)

This module provides the MCP (Model Context Protocol) interface for
the AI agent to interact with the SQLite-based memory system.

URI-based addressing with domain prefixes:
- core://agent              - AI's identity/memories
- writer://chapter_1             - Story/script drafts
- game://magic_system            - Game setting documents

Multiple paths can point to the same memory (aliases).
"""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from typing import Any, Dict, List, Optional, Tuple
import config as _cfg

# Ensure we can import from backend modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from db import (
    get_db_manager, get_graph_service, get_glossary_service,
    get_search_indexer, close_db, get_preset_service,
)
from db.namespace import get_namespace
from db.snapshot import get_changeset_store
from text_patch import (
    normalize_with_positions,
    find_valid_matches,
    try_normalized_patch,
    normalize_literal_newlines,
    format_normalization_preview,
)
from system_views import (
    fetch_and_format_memory,
    generate_boot_memory_view,
    generate_wakeup_view,
    generate_memory_index_view,
    generate_recent_memories_view,
    generate_glossary_index_view,
    generate_diagnostic_view,
)
import contextlib
from locales import t



from web_app import FRONTEND_DIR, build_web_app
FRONTEND_SRC = FRONTEND_DIR.parent


async def _ensure_frontend_built():
    """Auto-build the frontend dashboard on first run or when code updates."""
    if not (FRONTEND_SRC / "package.json").is_file():
        return
    if os.environ.get("SKIP_FRONTEND_BUILD", "").lower() in ("true", "1", "yes"):
        return
    if not shutil.which("npm"):
        print(t("startup.npm_not_found"), file=sys.stderr)
        return

    # Check version from package.json to detect frontend updates
    current_version = "unknown"
    try:
        package_json_path = FRONTEND_SRC / "package.json"
        if package_json_path.is_file():
            import json
            content = package_json_path.read_text(encoding="utf-8")
            pkg_data = json.loads(content)
            if "version" in pkg_data:
                current_version = pkg_data["version"]
    except Exception:
        pass

    build_marker = FRONTEND_DIR / ".build_version"
    
    if FRONTEND_DIR.is_dir():
        if build_marker.is_file():
            try:
                last_build_version = build_marker.read_text().strip()
                if last_build_version == current_version and current_version != "unknown":
                    return  # Up to date
            except Exception:
                pass
        # If marker is missing or doesn't match, we need to rebuild.

    print(t("startup.building"), file=sys.stderr)
    try:
        steps = [
            (t("startup.installing_deps"), "npm install --no-fund --no-audit"),
            (t("startup.compiling"), "npm run build"),
        ]

        for label, cmd in steps:
            print(t("startup.step_progress").format(label=label), file=sys.stderr)
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                cwd=str(FRONTEND_SRC),
                capture_output=True,
                text=True,
                shell=True,
            )
            if result.returncode != 0:
                err = result.stderr.strip() or result.stdout.strip()
                print(
                    t("startup.build_failed").format(
                        cmd=cmd, exit_code=result.returncode, error_msg=err),
                    file=sys.stderr,
                )
                return

        # Write the marker after successful build
        if current_version != "unknown" and FRONTEND_DIR.is_dir():
            build_marker.write_text(current_version)

        print(t("startup.admin_ready"), file=sys.stderr)
    except Exception as e:
        print(
            t("startup.build_error").format(error=str(e)),
            file=sys.stderr,
        )

def get_config() -> Dict[str, Any]:
    """Public accessor for config (used by system_views)."""
    return _cfg.get_all()



@contextlib.asynccontextmanager
async def lifespan(server: FastMCP):
    """Manage database connection lifecycle within the MCP event loop."""
    web_server = None
    web_task = None
    try:
        _cfg.ensure_config_exists()

        db_manager = get_db_manager()
        if os.environ.get("SKIP_DB_INIT", "").lower() not in ("true", "1", "yes"):
            await db_manager.init_db()

        # Auto-promote config.json boot_uris into presets table on first run
        preset_service = get_preset_service()
        await preset_service.auto_promote_from_config()

        # Launch frontend build in background so we don't block MCP handshake
        asyncio.create_task(_ensure_frontend_built())

        # In stdio mode, spin up an embedded HTTP server for the admin UI.
        # run_sse.py sets _NOCTURNE_SSE_MODE to prevent a duplicate.
        if not os.environ.get("_NOCTURNE_SSE_MODE"):
            import uvicorn
            from auth import enforce_network_auth

            port = int(_cfg.get("web_port"))
            web_host = _cfg.get("host")
            enforce_network_auth(host=web_host)
            @contextlib.asynccontextmanager
            async def embedded_lifespan(app):
                # The parent process (FastMCP lifespan) already owns DB init & close.
                # The embedded admin UI should not manage the database connection lifecycle.
                yield

            config = uvicorn.Config(
                build_web_app(lifespan=embedded_lifespan), host=web_host, port=port, log_level="warning",
            )
            web_server = uvicorn.Server(config)
            
            async def _serve_ui():
                try:
                    await web_server.serve()
                except Exception:
                    # Ignore the raw error message (usually OSError for address in use)
                    # and print a user-friendly explanation.
                    print(t("startup.port_in_use").format(port=port), file=sys.stderr)
                except SystemExit:
                    print(t("startup.port_in_use").format(port=port), file=sys.stderr)

            web_task = asyncio.create_task(_serve_ui())
            ui = f"http://localhost:{port}/"
            api_docs = f"http://localhost:{port}/api/docs"
            
            print(f"Admin UI:  {ui}", file=sys.stderr)
            print(f"REST API:  {api_docs}", file=sys.stderr)

            auto_open = _cfg.get("auto_open_browser")
            if auto_open:
                async def _open_browser():
                    while not getattr(web_server, "started", False):
                        if web_task.done():
                            return
                        await asyncio.sleep(0.1)
                    webbrowser.open(ui)
                asyncio.create_task(_open_browser())

        yield
    finally:
        if web_server:
            web_server.should_exit = True
        if web_task:
            await web_task
        await close_db()


# Initialize FastMCP server with the lifespan hook
mcp = FastMCP(
    "Nocturne Memory Interface",
    lifespan=lifespan,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False  # safe when behind a trusted reverse proxy
    ),
)

# =============================================================================
# Domain Configuration
# =============================================================================
# Valid domains (protocol prefixes)
# =============================================================================

def get_valid_domains() -> list[str]:
    raw = _cfg.get("valid_domains")
    domains = raw if isinstance(raw, list) else [
        d.strip() for d in str(raw).split(",") if d.strip()
    ]
    if "system" not in domains:
        domains.append("system")
    return domains
DEFAULT_DOMAIN = "core"
PUBLIC_READONLY_MCP = bool(_cfg.get("public_readonly_mcp"))



# =============================================================================
# URI Parsing
# =============================================================================

# Regex pattern for URI: domain://path
_URI_PATTERN = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)://(.*)$")


def parse_uri(uri: str) -> Tuple[str, str]:
    """
    Parse a memory URI into (domain, path).

    Supported formats:
    - "core://agent"          -> ("core", "agent")
    - "writer://chapter_1"         -> ("writer", "chapter_1")
    - "nocturne"              -> ("core", "nocturne")  [legacy fallback]

    Args:
        uri: The URI to parse

    Returns:
        Tuple of (domain, path)

    Raises:
        ValueError: If the URI format is invalid or domain is unknown
    """
    uri = uri.strip()

    match = _URI_PATTERN.match(uri)
    if match:
        domain = match.group(1).lower()
        path = match.group(2).strip("/")

        valid = get_valid_domains()
        if domain not in valid:
            raise ValueError(
                f"Unknown domain '{domain}'. Valid domains: {', '.join(valid)}"
            )

        return (domain, path)

    # Legacy fallback: bare path without protocol
    # Assume default domain (core)
    path = uri.strip("/")
    return (DEFAULT_DOMAIN, path)


def make_uri(domain: str, path: str) -> str:
    """
    Create a URI from domain and path.

    Args:
        domain: The domain (e.g., "core", "writer")
        path: The path (e.g., "nocturne")

    Returns:
        Full URI (e.g., "core://agent")
    """
    return f"{domain}://{path}"


# =============================================================================
# Changeset Helpers — before/after state capture with overwrite semantics
# =============================================================================


def _record_rows(
    before_state: Dict[str, List[Dict[str, Any]]],
    after_state: Dict[str, List[Dict[str, Any]]],
):
    """
    Feed row-level before/after states into the ChangesetStore.

    Overwrite semantics are handled by the store:
    - First touch of a PK: stores both before and after.
    - Subsequent touches: overwrites after only; before is frozen.

    Changes are written to the namespace-specific store so that each agent's
    review queue remains isolated.
    """
    store = get_changeset_store()
    store.record_many(before_state, after_state)


def write_tool():
    """Conditionally register mutating tools for public read-only deployments."""

    def decorator(func):
        if PUBLIC_READONLY_MCP:
            return func
        return mcp.tool()(func)

    return decorator



# =============================================================================
# MCP Tools — 面向角色的记忆界面
# =============================================================================
# 每个工具都以角色视角描述，统一使用 xxx_memory 命名
# =============================================================================

def _get_valid_domain_list() -> list[str]:
    """Get valid domains from config."""
    return get_valid_domains()


def _resolve_parent_children(graph, uri: str, namespace: str) -> Tuple[str, str]:
    """Parse URI and validate domain, returning (domain, path)."""
    domain, path = parse_uri(uri)
    valid = _get_valid_domain_list()
    if domain not in valid and domain not in ("history", "history_raw"):
        raise ValueError(f"Unknown domain '{domain}'. Valid: {', '.join(valid)}")
    return domain, path


# ── 查看 ──────────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_memory(uri: str) -> str:
    """查看一段记忆的内容。

    这是你回想起某件事的主要方式。输入 URI 就能看到那里的内容，包括子节点和相关的触发词关联。

    Args:
        uri: 记忆的 URI，例如 core://identity/habits

        特殊系统视图（不需要记忆也看得到）：
        - system://boot        : 醒来时最先看到的记忆
        - system://index/<domain>: 查看某个域下的所有记忆索引（如 system://index/core）
        - system://recent/<N>  : 查看最近修改的 N 条记忆（如 system://recent/10）
        - system://glossary    : 所有触发词索引
    """
    try:
        stripped = uri.strip()

        # ── System URI handling ────────────────────────────────────────────
        if stripped.startswith("system://"):
            parts = stripped[len("system://"):].split("/")
            cmd = parts[0].lower() if parts else ""

            if cmd == "boot":
                preset = get_preset_service()
                boot_uris = await preset.get_boot_uris(namespace=get_namespace())
                return await generate_boot_memory_view(boot_uris)

            elif cmd == "wakeup":
                preset = get_preset_service()
                boot_uris = await preset.get_boot_uris(namespace=get_namespace())
                history_limit = int(parts[1]) if len(parts) > 1 and parts[1] else 5
                return await generate_wakeup_view(boot_uris, history_limit)

            elif cmd == "memory-slot":
                slot_type = parts[1] if len(parts) > 1 else ""
                preset = get_preset_service()
                boot_uris = await preset.get_boot_uris(namespace=get_namespace())
                from system_views import generate_memory_slot_view
                return await generate_memory_slot_view(slot_type, boot_uris)

            elif cmd == "index":
                domain_filter = parts[1] if len(parts) > 1 else None
                return await generate_memory_index_view(domain_filter)

            elif cmd == "recent":
                limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
                return await generate_recent_memories_view(limit)

            elif cmd == "glossary":
                return await generate_glossary_index_view()

            elif cmd == "diagnostic":
                domain = parts[1] if len(parts) > 1 else DEFAULT_DOMAIN
                days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 30
                return await generate_diagnostic_view(domain, days)

            else:
                return f"未知的系统视图：{stripped}。试试 system://boot, system://wakeup, system://index/<domain>, system://recent/<N>, system://glossary, system://diagnostic/<domain>"

        # ── Normal memory lookup ───────────────────────────────────────────
        return await fetch_and_format_memory(stripped, track_access=True)

    except ValueError as e:
        return f"出错了：{str(e)}"
    except Exception as e:
        return f"出错了：{str(e)}"


@mcp.tool()
async def search_memory(query: str, domain: Optional[str] = None, limit: int = 10, sort_by_world: bool = False) -> str:
    """搜索记忆。想不起 URI 的时候用这个来找。

    这是全文搜索，不是语义搜索。输入关键词就能找到相关记忆。

    Args:
        query: 搜索关键词
        domain: 可选，限定在某个域名下搜索（如 "core"、"history"）
        limit: 最多返回多少条（默认 10）
        sort_by_world: 是否按世界时间排序（默认按现实时间）
    """
    graph = get_graph_service()

    try:
        from mcp_server import get_valid_domains
        valid = get_valid_domains()
        if domain is not None and domain not in valid:
            return f"没有 '{domain}' 这个域名。可用的：{', '.join(valid)}"

        results = await graph.search_memories(
            query, domain, limit=limit, namespace=get_namespace()
        )

        if sort_by_world:
            # Standardize dates for sorting, treat None as earliest
            results.sort(key=lambda x: x.get("world_timestamp") or "0000-00-00", reverse=True)

        if not results:
            scope = f"在 {domain}" if domain else "所有域名"
            return f"{scope}里没有找到和「{query}」相关的记忆。"

        lines = [f"找到了 {len(results)} 条和「{query}」相关的记忆：", ""]
        for item in results:
            from mcp_server import make_uri, DEFAULT_DOMAIN
            uri = item.get("uri", make_uri(item.get("domain", DEFAULT_DOMAIN), item["path"]))
            lines.append(f"- {uri}")
            lines.append(f"  重要性：{item['priority']}")
            if item.get("disclosure"):
                lines.append(f"  想起条件：{item['disclosure']}")
            lines.append(f"  {item['snippet']}")
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return f"搜索出错了：{str(e)}"


@mcp.tool()
async def remember_memory(uri: str, content: str, time: Optional[str] = None) -> str:
    """记下一段新的记忆。

    Args:
        uri: 记忆的路径（URI），例如 core://identity
        content: 记忆的具体内容
        time: 可选。事件发生的世界时间（YYYY-MM-DD）。
              支持相对位移，如 "-1d"（昨天）, "+1y"（明年）。
              【何时使用时间？】
              - Events：需要时间线追踪的具体事件必须写明时间。例如某次相遇、交流（如 core://events/first_impression）。
              - Static：背景故事、性格习惯、世界观规则、常识（如 core://identity, core://world, core://relationships）。这类信息是永久有效的，无需传入时间。
    """
    try:
        # Split URI into domain, parent_path, and title
        domain, full_path = parse_uri(uri)
        if "/" in full_path:
            parent_path, title = full_path.rsplit("/", 1)
        else:
            parent_path = ""
            title = full_path

        # Handle world time parsing
        final_world_time = None
        config = get_config()
        clock = config.get("world_clock", {})
        current_world_time = clock.get("current_time")

        if time:
            from system_views import parse_relative_offset
            # Try parsing as offset first
            offset_date = parse_relative_offset(time, current_world_time)
            final_world_time = offset_date or time
        elif clock.get("auto_timestamp") and current_world_time:
            final_world_time = current_world_time

        graph = get_graph_service()
        await graph.create_memory(
            parent_path, content, priority=5, title=title, domain=domain, 
            namespace=get_namespace(),
            world_timestamp=final_world_time
        )
        
        return f"已记下记忆: {uri}" + (f" (发生于 {final_world_time})" if final_world_time else "")
    except Exception as e:
        return f"记录失败: {str(e)}"


@mcp.tool()
async def set_world_time(time: str) -> str:
    """设置当前世界时间。

    改变此设置后，后续创建的记忆会自动关联到新时间，且在查看记忆时会更新“N天前”的计算参考。

    Args:
        time: 世界观日期（如 2024-06-05）或相对偏移量（如 "+1d"）。
    """
    try:
        config_data = get_config()
        clock = config_data.get("world_clock", {})
        current_time = clock.get("current_time", "2024-06-01")
        
        from system_views import parse_relative_offset
        new_time = parse_relative_offset(time, current_time) or time
        
        clock["current_time"] = new_time
        _cfg.set_value("world_clock", clock)
        
        return f"当前世界时间已设置为: {new_time}"
    except Exception as e:
        return f"设置失败: {str(e)}"

@mcp.tool()
async def remember_child_memory(
    parent_uri: str, 
    content: str, 
    importance: int = 5, 
    when: str = "", 
    title: Optional[str] = None,
    time: Optional[str] = None,
) -> str:
    """
    把一段新的记忆放在某个已有的父节点下。父节点通常是你自然
    会想到的那件事——当你想起来父节点的时候，这个子节点也会浮现。

    每条记忆都需要一个「什么时候会想起」的条件(when)，不然它
    就永远藏在你脑子里找不到。

    Args:
        parent_uri: 父节点的 URI。放在哪个已有的记忆下面？
                    如果放在域名根目录，用 "core://" 这样的格式。
        content: 记忆的内容。想记什么就写什么。
        importance: 重要性（0=最重要，数字越大越次要）。
                     参考尺度：
                     - 0：绝对不能忘的事
                     - 1：很重要的事
                     - 5：普通的事
                     - 10：边角料
        when: 什么情况下会想起这件事。
              写一个具体的外部信号或对话情境——别人说什么、或者你想做什么的时候。
              错误的例子：「当我觉得/意识到/注意到……」（意识不到就晚了）
              正确的例子：「当对方提到晚饭、或者表现出饥饿时」（外部信号，来得早）
        title: 可选的标题。一两个词概括内容，方便你以后扫一眼就知道是什么。
                只能用字母、数字、连字符和下划线。
        time: 可选。事件发生的世界时间（YYYY-MM-DD 或相对位移如 "-1d"）。
              如果没有提供，系统会优先尝试继承父节点的世界时间。

    Returns:
        新建记忆的 URI
    """
    graph = get_graph_service()

    try:
        if not when or not when.strip():
            return "每条记忆都需要一个「什么时候会想起」的条件(when)。不写的话这条记忆就永远找不到了。"

        if title:
            if not re.match(r"^[a-zA-Z0-9_-]+$", title):
                return "标题只能包含字母、数字、连字符和下划线（不能有空格、斜杠、特殊字符）。"

        domain, parent_path = parse_uri(parent_uri)

        # --- 时间解析与隐式继承逻辑 ---
        final_world_time = None
        config = get_config()
        clock = config.get("world_clock", {})
        current_world_time = clock.get("current_time")

        if time:
            from system_views import parse_relative_offset
            offset_date = parse_relative_offset(time, current_world_time)
            final_world_time = offset_date or time
        else:
            # 优先尝试从父节点继承世界时间
            parent_mem = await graph.get_memory_by_path(parent_path, domain, namespace=get_namespace())
            if parent_mem and parent_mem.get("world_timestamp"):
                final_world_time = parent_mem.get("world_timestamp")
            elif clock.get("auto_timestamp") and current_world_time:
                # 回退到全局当前时间
                final_world_time = current_world_time

        result = await graph.create_memory(
            parent_path=parent_path,
            content=content,
            priority=importance,
            title=title,
            disclosure=when,
            domain=domain,
            namespace=get_namespace(),
            world_timestamp=final_world_time, # 注入时间
        )

        created_uri = result.get("uri", make_uri(domain, result["path"]))
        _record_rows(before_state={}, after_state=result.get("rows_after", {}))

        msg = f"记住了：「{created_uri}」"
        if final_world_time:
            msg += f" (发生于 {final_world_time})"
            
        if result.get("path"):
            msg += f"\n\n新记的事已经放好了。你看看和它相关的其他记忆有没有什么要整理的？"
        return msg

    except ValueError as e:
        return f"没记住：{str(e)}"
    except Exception as e:
        return f"没记住：{str(e)}"


# ── 修改 ──────────────────────────────────────────────────────────────────

@write_tool()
async def edit_memory(
    uri: str,
    old_text: Optional[str] = None,
    new_text: Optional[str] = None,
    append: Optional[str] = None,
    line: Optional[int] = None,
    line_content: Optional[str] = None,
    importance: Optional[int] = None,
    when: Optional[str] = None,
    time: Optional[str] = None,
) -> str:
    """修改一段记忆的内容。

    支持三种编辑方式（三选一）：

    1. 替换模式：old_text → new_text
       在现有内容中查找一段文字并替换。old_text 必须在内容中出现且唯一。
       如果 new_text 为空字符串，就是删除这一段。

    2. 追加模式：append
       在内容末尾添加新文字。

    3. 行编辑模式：line + line_content
       替换指定行的内容。行号从 1 开始。

    Args:
        uri: 要修改的记忆 URI
        old_text: [替换模式] 要改掉的原文（必须在内容中出现一次且唯一）
        new_text: [替换模式] 改成什么
        append: [追加模式] 追加到末尾的文字
        line: [行编辑] 要替换的行号（从 1 开始）
        line_content: [行编辑] 这一行的新内容
        importance: 可选，修改重要性
        when: 可选，修改想起条件
        time: 可选，修改该记忆发生的世界时间（如 "2024-06-01" 或 "-1d"）
    Examples:
        edit_memory("core://identity/habits", old_text="每天喝咖啡", new_text="每天喝茶")
        edit_memory("core://events/encounter_0302", append="\\n今天（3月3日）又遇到了他……")
        edit_memory("diary://0521_special_day", line=3, line_content="新的第三行内容")
        edit_memory("core://schedule", importance=2)  # 只改重要性
    """
    graph = get_graph_service()

    # Handle world time parsing
    final_world_time = None
    if time:
        config = get_config()
        clock = config.get("world_clock", {})
        current_world_time = clock.get("current_time")
        from system_views import parse_relative_offset
        offset_date = parse_relative_offset(time, current_world_time)
        final_world_time = offset_date or time


    try:
        domain, path = parse_uri(uri)
        full_uri = make_uri(domain, path)

        # ── 校验参数互斥 ──
        modes = 0
        if old_text is not None: modes += 1
        if append is not None: modes += 1
        if line is not None: modes += 1
        if modes > 1:
            return "不能同时使用多种编辑模式。请选一种：替换(old_text+new_text)、追加(append)、行编辑(line+line_content)。"
        if old_text is not None and new_text is None:
            return '替换模式需要 old_text 和 new_text 两个参数。要删除的话用 new_text=""。'
        if line is not None and line_content is None and importance is None and when is None:
            return "行编辑模式下需要提供 line_content（新内容）。"
        if line_content is not None and line is None:
            return "给了 line_content 但没给 line 行号。"
        if old_text is None and append is None and line is None and importance is None and when is None and time is None:
            return "没有要改的东西。至少提供一个编辑参数或修改时间/重要性/想起条件。"

        # ── 读取当前内容 ──
        memory = await graph.get_memory_by_path(path, domain, namespace=get_namespace())
        if not memory:
            return f"没找到「{full_uri}」这条记忆。"

        current_content = memory.get("content", "")
        content = None

        if old_text is not None:
            # 替换模式
            if old_text == new_text:
                return "old_text 和 new_text 一模一样，没任何变化。"

            count = current_content.count(old_text)
            if count > 1:
                return f"「{old_text}」在记忆里出现了 {count} 次，无法确定替换哪个。多写点上下文让它唯一。"
            if count == 1:
                content = current_content.replace(old_text, new_text, 1)
            else:
                # 尝试 \\n 规范化
                norm_old = normalize_literal_newlines(old_text) if "\\n" in old_text else None
                if norm_old is not None and norm_old != old_text:
                    norm_count = current_content.count(norm_old)
                    if norm_count == 1:
                        norm_new = normalize_literal_newlines(new_text) if new_text and "\\n" in new_text else new_text
                        content = current_content.replace(norm_old, norm_new, 1)

                if content is None:
                    # 尝试 Unicode 标准化匹配
                    patched = try_normalized_patch(current_content, old_text, new_text)
                    if patched is not None:
                        content = patched

                if content is None:
                    return f"在「{full_uri}」里没找到「{old_text}」。先 browse_memory 看看确切内容再试。"

            if content == current_content:
                return "替换后内容和原来一模一样，没有变化。"

        elif append is not None:
            # 追加模式
            if not append:
                return "追加的内容不能为空。"
            content = current_content + append

        elif line is not None:
            # 行编辑模式
            lines = current_content.split("\n")
            if line < 1 or line > len(lines):
                return f"行号 {line} 超出范围。这个记忆一共有 {len(lines)} 行。"
            lines[line - 1] = line_content
            content = "\n".join(lines)

        result = await graph.update_memory(
            path=path,
            content=content,
            priority=importance,
            disclosure=when,
            domain=domain,
            namespace=get_namespace(),
            world_timestamp=final_world_time,
        )

        _record_rows(
            before_state=result.get("rows_before", {}),
            after_state=result.get("rows_after", {}),
        )

        msg = f"已经改好了：「{full_uri}」"
        if final_world_time:
            msg += f" (时间更新为: {final_world_time})"
        return msg

    except ValueError as e:
        return f"没改掉：{str(e)}"
    except Exception as e:
        return f"没改掉：{str(e)}"


# ── 删除 ──────────────────────────────────────────────────────────────────

@write_tool()
async def forget_memory(uri: str) -> str:
    """忘掉一段记忆。删除前会自动备份到 staging/ 目录。

    删除的是这个 URI 路径下的记录。如果这个记忆还有其他入口
    （别名），只拆掉这一个入口，内容还在。如果是最后一个入口，记忆本身也会被删除。

    如果记忆下面还有子节点，得先把子节点清理掉才能删。

    Args:
        uri: 要删除的 URI，如 "core://items/old_book"

    Examples:
        forget_memory("core://observations/white_cat")
        forget_memory("diary://0521_old")
    """
    graph = get_graph_service()

    try:
        domain, path = parse_uri(uri)
        full_uri = make_uri(domain, path)

        memory = await graph.get_memory_by_path(path, domain, namespace=get_namespace())
        if not memory:
            return f"没找到「{full_uri}」这条记忆。"

        result = await graph.remove_path(path, domain, namespace=get_namespace())
        rows_before = result.get("rows_before", {})

        _record_rows(
            before_state=rows_before,
            after_state={},
        )

        deleted_path_count = len(rows_before.get("paths", []))
        descendant_count = max(0, deleted_path_count - 1)
        msg = f"忘掉了「{full_uri}」"
        if descendant_count > 0:
            msg += f"（连带清掉了 {descendant_count} 个子节点）"

        return msg

    except ValueError as e:
        return f"没忘掉：{str(e)}"
    except Exception as e:
        return f"没忘掉：{str(e)}"


# ── 关联 ──────────────────────────────────────────────────────────────────

@write_tool()
async def link_memory(
    target_uri: str,
    new_uri: str,
    importance: int,
    when: str,
) -> str:
    """同一条记忆多放一个入口。

    不是复制内容，只是在另一个位置开一扇门，指向同一条记忆。
    两个入口共享内容——改一个另一个也跟着变。
    子节点会自动继承，不用手动一个个加别名。

    什么时候用：
    - 一件事放在 A 下面想不起来，但在 B 下面就能自然想到
      → 在 B 下面加个别名指向 A
    - 想给记忆搬家：先加别名指向新位置，再 forget_memory 老位置

    Args:
        target_uri: 已有的记忆（要被指向的目标）
        new_uri: 新入口放哪里
        importance: 从这个入口想起时的重要性
        when: 从这入口什么时候会想起来

    Examples:
        link_memory("core://events/0322_first_encounter", "core://observations/Tina", importance=1, when="当提到对缇娜的第一印象时")
        link_memory("core://relationships/Tina", "core://observations/Tina", importance=3, when="当说起缇娜时")
    """
    graph = get_graph_service()

    try:
        new_domain, new_path = parse_uri(new_uri)
        target_domain, target_path = parse_uri(target_uri)

        result = await graph.add_path(
            new_path=new_path,
            target_path=target_path,
            new_domain=new_domain,
            target_domain=target_domain,
            priority=importance,
            disclosure=when,
            namespace=get_namespace(),
        )

        _record_rows(
            before_state={},
            after_state=result.get("rows_after", {}),
        )

        alias_uri = result.get("new_uri", new_uri)
        msg = f"在「{alias_uri}」也能想起「{target_uri}」了。"

        return msg

    except ValueError as e:
        return f"没加上：{str(e)}"
    except Exception as e:
        return f"没加上：{str(e)}"


@write_tool()
async def tag_memory(
    uri: str,
    add: Optional[List[str]] = None,
    remove: Optional[List[str]] = None,
) -> str:
    """给一段记忆贴上触发词标签。

    贴上标签后，当其他记忆的内容里出现这个词时，这条记忆会被
    关联显示出来。

    标签是和记忆内容绑定的（所有别名共享同一套标签），而不是和入口绑定的。

    怎么选标签词：
    - 这个词必须已经在某条旧记忆的内容里出现过
    - 用具体的词，太宽泛的（比如「重要」「东西」）会产生大量噪音
    - 一条记忆可以有多个标签，同一个词也可以指向多条记忆

    查看所有标签：browse_memory("system://glossary")

    Args:
        uri: 要贴标签的记忆（任何别名都行，指向同一条记忆）
        add: 要加的标签词列表（可选）
        remove: 要删的标签词列表（可选）

    Examples:
        tag_memory("core://identity/habits", add=["吃零食", "打游戏"])
        tag_memory("core://events/0316_small_talk", remove=["旧的标签"])
    """
    graph = get_graph_service()
    glossary = get_glossary_service()

    try:
        domain, path = parse_uri(uri)
        full_uri = make_uri(domain, path)

        memory = await graph.get_memory_by_path(path, domain, namespace=get_namespace())
        if not memory:
            return f"没找到「{full_uri}」。"

        node_uuid = memory["node_uuid"]

        if add and remove:
            add_set = {k.strip() for k in add if k.strip()}
            remove_set = {k.strip() for k in remove if k.strip()}
            overlap = add_set.intersection(remove_set)
            if overlap:
                return f"不能同时添加和删除同一个词：{', '.join(sorted(overlap))}"

        added = []
        skipped_add = []
        removed = []
        skipped_remove = []
        before_state = {"glossary_keywords": []}
        after_state = {"glossary_keywords": []}

        if add:
            for kw in add:
                kw = kw.strip()
                if not kw:
                    continue
                try:
                    result = await glossary.add_glossary_keyword(kw, node_uuid, namespace=get_namespace())
                    added.append(kw)
                    if "rows_before" in result:
                        before_state["glossary_keywords"].extend(result["rows_before"].get("glossary_keywords", []))
                    if "rows_after" in result:
                        after_state["glossary_keywords"].extend(result["rows_after"].get("glossary_keywords", []))
                except ValueError:
                    skipped_add.append(kw)

        if remove:
            for kw in remove:
                kw = kw.strip()
                if not kw:
                    continue
                result = await glossary.remove_glossary_keyword(kw, node_uuid, namespace=get_namespace())
                if result.get("success"):
                    removed.append(kw)
                    if "rows_before" in result:
                        before_state["glossary_keywords"].extend(result["rows_before"].get("glossary_keywords", []))
                    if "rows_after" in result:
                        after_state["glossary_keywords"].extend(result["rows_after"].get("glossary_keywords", []))
                else:
                    skipped_remove.append(kw)

        if added or removed:
            get_changeset_store().record_many(before_state, after_state)

        current = await glossary.get_glossary_for_node(node_uuid, namespace=get_namespace())

        lines = [f"「{full_uri}」的标签："]
        if added:
            lines.append(f"  加上了：{', '.join(added)}")
        if skipped_add:
            lines.append(f"  已经有了（跳过）：{', '.join(skipped_add)}")
        if removed:
            lines.append(f"  删掉了：{', '.join(removed)}")
        if skipped_remove:
            lines.append(f"  本来就没有（跳过）：{', '.join(skipped_remove)}")
        if current:
            lines.append(f"  现在是：{', '.join(current)}")
        else:
            lines.append("  现在没有标签。")

        return "\n".join(lines)

    except ValueError as e:
        return f"标签没改：{str(e)}"
    except Exception as e:
        return f"标签没改：{str(e)}"


# ── 整理 ──────────────────────────────────────────────────────────────────

@write_tool()
async def merge_memories(
    uris: List[str],
    target_uri: str,
    content: str,
    reason: Optional[str] = None,
) -> str:
    """把多条记忆合并成一条。

    当你发现好几段记忆其实是在说同一件事的时候，就可以把它们合起来。
    合并后源头记忆会被删除（自动备份到 staging/ 目录），
    所有旧标签会集中到新记忆上。

    步骤：
    1. 读取所有源记忆
    2. 用你写的新内容创建目标记忆
    3. 把源记忆上的标签转移到目标
    4. 删除源记忆（带备份）

    Args:
        uris: 要合并的多条记忆 URI 列表
        target_uri: 合并后放在哪里
        content: 合并后的完整内容（你来总结）
        reason: 为什么要合并（可选，会写在结果里方便以后回顾）

    Examples:
        merge_memories(["core://events/0301_first_impression_Tina", "core://events/0302_small_talk_Tina"], "core://events/Tina", "缇娜这段时候给我留下了不错的印象……", reason="这几天的事都和缇娜有关")
    """
    graph = get_graph_service()
    glossary = get_glossary_service()

    try:
        if len(uris) < 2:
            return "至少需要两条记忆才能合并。"

        target_domain, target_path = parse_uri(target_uri)
        namespace = get_namespace()

        # 1. 读取所有源记忆
        sources = []
        source_glossary_keywords = []
        for uri in uris:
            domain, path = parse_uri(uri)
            memory = await graph.get_memory_by_path(path, domain, namespace=namespace)
            if not memory:
                return f"没找到源记忆「{uri}」。"
            sources.append((domain, path, memory))
            # 收集标签
            node_glossary = await glossary.get_glossary_for_node(memory["node_uuid"], namespace=namespace)
            source_glossary_keywords.extend(node_glossary)

        # 2. 创建目标记忆
        parent_path = "/".join(target_path.split("/")[:-1])
        title_part = target_path.split("/")[-1]

        result = await graph.create_memory(
            parent_path=parent_path,
            content=content,
            priority=3,
            title=title_part,
            disclosure="当需要回想合并后的事时",
            domain=target_domain,
            namespace=namespace,
        )

        target_node_uuid = result.get("node_uuid")
        created_uri = result.get("uri", make_uri(target_domain, result["path"]))

        # 3. 转移标签到目标节点
        if target_node_uuid and source_glossary_keywords:
            added_keywords = set()
            for kw in source_glossary_keywords:
                if kw not in added_keywords:
                    try:
                        await glossary.add_glossary_keyword(kw, target_node_uuid, namespace=namespace)
                        added_keywords.add(kw)
                    except ValueError:
                        pass

        # 4. 删除源记忆（逐条删除）
        deleted_sources = []
        for domain, path, memory in sources:
            full_uri = make_uri(domain, path)
            try:
                await graph.remove_path(path, domain, namespace=namespace)
                deleted_sources.append(full_uri)
            except Exception as e:
                # 单条删除失败不阻断整体流程
                pass

        _record_rows(
            before_state=result.get("rows_before", {}),
            after_state=result.get("rows_after", {}),
        )

        msg = f"合并完成：{len(uris)} 条记忆 → 「{created_uri}」"
        if reason:
            msg += f"\n原因：{reason}"
        if deleted_sources:
            msg += f"\n已删除旧入口：{len(deleted_sources)} 条"
        if source_glossary_keywords:
            transferred = len(set(source_glossary_keywords))
            msg += f"\n转移了 {transferred} 个标签到新记忆"

        return msg

    except ValueError as e:
        return f"合并没成功：{str(e)}"
    except Exception as e:
        return f"合并没成功：{str(e)}"


@write_tool()
async def organize_memory(
    target_uri: str,
    source_uris: List[str],
    content: str,
    mode: str = "move",
    importance: int = 3,
    when: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> str:
    """把几段相关的记忆整理成一个主题。

    当你发现几段零散的记忆其实属于同一个主题时，可以用这个工具
    把它们归纳到一起。有三种整理方式：

    - move（默认）：创建主题摘要，把源记忆放到主题下，再删掉源入口
    - link：创建主题摘要，给源记忆加一个主题下的入口，保留原位置
    - keep：只创建主题摘要，不动源记忆

    Args:
        target_uri: 主题放在哪里
        source_uris: 要整理的相关记忆
        content: 主题总结（你对这个主题的整体理解）
        mode: 整理方式——"move"、"link" 或 "keep"（默认 move）
        importance: 主题的重要性（默认 3）
        when: 什么时候会想到这个主题
        tags: 可选，给主题加上标签词

    Examples:
        organize_memory("core://话题/关于他", ["core://碎片/对话1", "core://碎片/他说过的话"], "我对他的整体印象……", mode="move", tags=["他", "朋友"])
    """
    graph = get_graph_service()
    glossary = get_glossary_service()

    try:
        if not source_uris:
            return "至少需要一条源记忆来整理。"

        if mode not in ("move", "link", "keep"):
            return "mode 必须是 move、link 或 keep。"

        target_domain, target_path = parse_uri(target_uri)
        namespace = get_namespace()

        # 确定主题的父路径和标题
        parent_path = "/".join(target_path.split("/")[:-1]) if "/" in target_path else ""
        title_part = target_path.split("/")[-1]

        # 1. 创建主题总结节点
        result = await graph.create_memory(
            parent_path=parent_path,
            content=content,
            priority=importance,
            title=title_part,
            disclosure=when or f"当说到{title_part}时",
            domain=target_domain,
            namespace=namespace,
        )

        target_node_uuid = result.get("node_uuid")
        topic_uri = result.get("uri", make_uri(target_domain, result["path"]))

        # 2. 给主题加标签
        if tags and target_node_uuid:
            for kw in tags:
                kw = kw.strip()
                if kw:
                    try:
                        await glossary.add_glossary_keyword(kw, target_node_uuid, namespace=namespace)
                    except ValueError:
                        pass

        # 3. 处理源记忆
        linked = 0
        moved = 0
        for src_uri in source_uris:
            src_domain, src_path = parse_uri(src_uri)
            src_basename = src_path.split("/")[-1]
            # 源记忆成为主题的子节点
            child_path = f"{target_path}/{src_basename}"

            try:
                await graph.add_path(
                    new_path=child_path,
                    target_path=src_path,
                    new_domain=target_domain,
                    target_domain=src_domain,
                    priority=importance + 1,
                    disclosure=when or f"当说起{src_basename}时",
                    namespace=namespace,
                )
                linked += 1

                if mode == "move":
                    await graph.remove_path(src_path, src_domain, namespace=namespace)
                    moved += 1

            except Exception:
                pass

        _record_rows(
            before_state={},
            after_state=result.get("rows_after", {}),
        )

        msg_parts = [f"整理好了：「{topic_uri}」"]
        if linked:
            msg_parts.append(f"  关联了 {linked} 条记忆到主题下")
        if moved:
            msg_parts.append(f"  移除了 {moved} 个旧入口")
        if tags:
            msg_parts.append(f"  标签：{', '.join(tags)}")

        return "\n".join(msg_parts)

    except ValueError as e:
        return f"没整理好：{str(e)}"
    except Exception as e:
        return f"没整理好：{str(e)}"


# ── 存档 ──────────────────────────────────────────────────────────────────

@write_tool()
async def archive_memory(
    title: str,
    history: str,
    mode: str = "char",
    raw: Optional[str] = None,
    time: Optional[str] = None,
) -> str:
    """把刚才发生的事存档到历史记录里。

    每轮对话或场景结束后，用这个工具把发生了什么记到 history 域。
    之后就可以通过「system://wakeup」来回想最近发生的事。

    Args:
        title: 场景的简短标题（如 "tavern_brawl" 或 "meet_tina"）。
               只能包含字母、数字、下划线和连字符。这会成为记忆的路径。
        history: 场景摘要。整理过的、这段场景里发生了什么。
        mode: "char"（角色视角）或 "gm"（GM视角），默认 "char"。
        raw: 原始记录。可选，完整的对话或事件记录。
        time: 可选。存档对应的世界时间（如 "2024-06-01" 或 "-1d"）。默认使用当前世界时间。
    """
    graph = get_graph_service()

    try:
        import re
        namespace = get_namespace()

        if not history.strip():
            return "history 不能为空。写一下刚才发生了什么。"
            
        if not title or not re.match(r"^[a-zA-Z0-9_-]+$", title):
            return "title 必须提供，且只能包含字母、数字、连字符和下划线（如 'first_encounter'）。"

        # --- 世界时间处理 ---
        config = get_config()
        clock = config.get("world_clock", {})
        current_world_time = clock.get("current_time")

        final_world_time = None
        if time:
            from system_views import parse_relative_offset
            offset_date = parse_relative_offset(time, current_world_time)
            final_world_time = offset_date or time
        elif current_world_time:
            final_world_time = current_world_time

        # 写入 history 域（统一放在 scenes/ 目录下保持整洁）
        await graph.create_memory(
            parent_path="scenes",
            content=history,
            priority=5,
            title=title,
            disclosure="当回顾最近经历时",
            domain="history",
            namespace=namespace,
            world_timestamp=final_world_time, 
        )

        if raw and raw.strip():
            await graph.create_memory(
                parent_path="scenes",
                content=raw,
                priority=5,
                title=f"{title}_raw",
                disclosure="",
                domain="history_raw",
                namespace=namespace,
                world_timestamp=final_world_time, 
            )

        msg = f"场景已存档（{mode}）：history://scenes/{title}"
        if final_world_time:
            msg += f" (世界时间: {final_world_time})"
            
        return msg

    except ValueError as e:
        return f"存档失败：{str(e)}"
    except Exception as e:
        return f"存档失败：{str(e)}"

# ── 回顾 ──────────────────────────────────────────────────────────────────

@mcp.tool()
async def recent_memories(limit: int = 10, domain: Optional[str] = None) -> str:
    """看看最近发生了什么——最近改过的记忆。

    列出最近新增或修改的记忆，按时间倒序。
    可以用来快速回顾最近你在想什么、记了什么。

    Args:
        limit: 最多显示多少条（默认 10，最多 50）
        domain: 可选，只看某个域名的（如 "core"）

    Examples:
        recent_memories()           # 最近 10 条
        recent_memories(20)          # 最近 20 条
        recent_memories(domain="core")  # 只看核心记忆
    """
    try:
        return await generate_recent_memories_view(limit)
    except Exception as e:
        return f"获取最近记忆失败：{str(e)}"


@write_tool()
async def boot_memory(
    action: str,
    uris: Optional[List[str]] = None,
) -> str:
    """管理「醒来记忆」——你醒来时最先想起的事。

    「醒来记忆」是你每次重新进入世界时最先看到的记忆，
    相当于你放在床头的东西。

    action 操作：
    - list：查看当前的醒来记忆列表
    - set：完全替换成新的列表
    - add：在现有列表末尾加上一条
    - remove：从列表中移除一条

    Args:
        action: "list" | "set" | "add" | "remove"
        uris: set/add/remove 时要操作的 URI 列表

    Examples:
        boot_memory("list")                    # 看看现在记得什么
        boot_memory("add", ["core://identity"])  # 把这件事放在床头
        boot_memory("remove", ["core://events/0211_hot_coffee"])   # 不再自动想起了
        boot_memory("set", ["core://最重要的", "core://第二重要的"])  # 重新排
    """
    preset = get_preset_service()

    try:
        namespace = get_namespace()

        if action == "list":
            current = await preset.get_boot_uris(namespace=namespace)
            if not current:
                return "现在没有设置醒来记忆。用 boot_memory('add', [...]) 来设置。"
            lines = [f"醒来时会想起 {len(current)} 件事：", ""]
            for i, uri in enumerate(current, 1):
                lines.append(f"{i}. {uri}")
            # 读取内容预览
            graph = get_graph_service()
            for i, uri in enumerate(current, 1):
                try:
                    domain, path = parse_uri(uri)
                    memory = await graph.get_memory_by_path(path, domain, namespace=namespace)
                    if memory and memory.get("content"):
                        snippet = memory["content"].strip()[:100].replace("\n", " ")
                        lines.append(f"   → {snippet}…" if len(memory["content"]) > 100 else f"   → {snippet}")
                except Exception:
                    pass
            return "\n".join(lines)

        elif action == "set":
            if not uris:
                return "set 操作需要提供 uris 列表。"
            await preset.set_boot_uris(namespace=namespace, uris=uris)
            return f"醒来记忆已设为 {len(uris)} 条。"

        elif action == "add":
            if not uris:
                return "add 操作需要提供 uris 列表。"
            current = await preset.get_boot_uris(namespace=namespace)
            existing = set(current)
            added = [u for u in uris if u not in existing]
            if not added:
                return "这些 URI 已经在醒来记忆里了。"
            current.extend(added)
            await preset.set_boot_uris(namespace=namespace, uris=current)
            return f"加上了 {len(added)} 条醒来记忆。"

        elif action == "remove":
            if not uris:
                return "remove 操作需要提供 uris 列表。"
            current = await preset.get_boot_uris(namespace=namespace)
            remove_set = set(uris)
            remaining = [u for u in current if u not in remove_set]
            if len(remaining) == len(current):
                return "这些 URI 不在醒来记忆列表里。"
            await preset.set_boot_uris(namespace=namespace, uris=remaining)
            return f"移除了 {len(current) - len(remaining)} 条醒来记忆。"

        else:
            return "action 必须是 list、set、add 或 remove。"

    except ValueError as e:
        return f"没改掉：{str(e)}"
    except Exception as e:
        return f"没改掉：{str(e)}"


# =============================================================================
# MCP Resources
# =============================================================================


if __name__ == "__main__":
    mcp.run()