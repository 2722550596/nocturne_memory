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
from db.namespace import get_namespace, namespace_scope
from db.snapshot import get_changeset_store, commit_checkpoint
from models.mcp_results import (
    ToolResult, CreateResult, UpdateResult, ForgetResult,
    LinkResult, TagResult, ArchiveResult
)
from db.models import PLACEHOLDER_CONTENT
from sqlalchemy.exc import IntegrityError
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
    generate_timeline_view,
    generate_forgotten_view,
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


# ── 批量工具共享辅助 ───────────────────────────────────────────────────────

# 占位内容常量定义在 db.models，供 graph / system_views / mcp_server 共用。

def _format_placeholder_notice(domain: str, placeholders: List[str]) -> str:
    """把新建的占位父节点格式化成一条回写提醒。

    任何会顺带建出占位祖先的工具都该把这段话拼进返回消息里：
    内容先落地了，但占位父节点还是空的，模型需要回头补真内容。
    """
    if not placeholders:
        return ""
    stub_uris = "、".join(f"{domain}://{p}" for p in placeholders)
    return (
        f"\n\n注意：写入时父节点还不存在，已先建了占位节点 {stub_uris}"
        f"（内容为「{PLACEHOLDER_CONTENT}」）。"
        f"\n等一下请回写这些父节点的真实内容：edit_memory(uri=..., new_text=\"...\")。"
    )


async def _ensure_parent_chain(
    graph, domain: str, parent_path: str, namespace: str
) -> List[str]:
    """逐级补齐缺失的祖先路径，缺的一律建成占位容器节点。

    记 `core://a/b/c` 而 `a`、`a/b` 都还不存在时，先把这条链补出来，
    让子节点能立刻落地。占位节点内容是 PLACEHOLDER_CONTENT，调用方
    需要在返回给模型的结果里提醒稍后回写真实内容。

    并发说明：模型常在同一轮里同时写父节点和子节点（如 core://events
    与 core://events/lucas），两个请求都会走到这里。检查「父不存在」和
    插入占位之间存在竞窗：并发方可能恰好在这中间插进了同一路径，让
    create_memory 以 UNIQUE 冲突或 "already exists" 失败。这不视为错误——
    只要插进去的那条确实是个节点，父链就算补齐了，继续往下走。
    """
    if not parent_path:
        return []

    created: List[str] = []
    current = ""
    for segment in parent_path.split("/"):
        current = f"{current}/{segment}" if current else segment
        if await graph.get_memory_by_path(current, domain, namespace=namespace):
            continue
        try:
            await graph.create_memory(
                parent_path=current.rsplit("/", 1)[0] if "/" in current else "",
                content=PLACEHOLDER_CONTENT,
                priority=8,
                title=segment,
                disclosure="",
                domain=domain,
                namespace=namespace,
            )
            created.append(current)
        except (ValueError, IntegrityError):
            # 并发方抢先建了这条路径。重读确认它真实存在（且不是我们
            # 自己的写失败残影），存在即视为父链已补齐。
            existing = await graph.get_memory_by_path(current, domain, namespace=namespace)
            if not existing:
                raise
    return created


async def _create_or_upgrade_memory(
    graph,
    domain: str,
    path: str,
    content: str,
    priority: int,
    disclosure: Optional[str] = None,
    namespace: str = "",
    world_timestamp: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """创建记忆；目标路径被占位节点占着时，把真内容升级进去。

    并发反例：模型同一轮同时写 core://events（真内容）和
    core://events/lucas。若子节点那一路先把占位父链建完提交，
    core://events 会先以占位形态落地，随后父节点那一路的
    create_memory 就撞「already exists」，真内容直接丢失——比撞
    UNIQUE 的另一方向严重得多。这里把碰撞接住：目标还是占位，
    就用 update_memory 把真内容和优先级写进去（占位升级）；已有
    真内容则原样报错，绝不静默覆盖。

    返回 (create_memory 兼容的结果 dict, 是否发生了升级)。
    """
    if "/" in path:
        parent_path, title = path.rsplit("/", 1)
    else:
        parent_path, title = "", path
    try:
        result = await graph.create_memory(
            parent_path, content, priority=priority, title=title,
            disclosure=disclosure, domain=domain, namespace=namespace,
            world_timestamp=world_timestamp,
        )
        return result, False
    except (ValueError, IntegrityError):
        existing = await graph.get_memory_by_path(path, domain, namespace=namespace)
        if not existing or existing.get("content") != PLACEHOLDER_CONTENT:
            # 真的重复写入（或并发残影），按原语义报错。
            raise
        upgraded = await graph.update_memory(
            path, content, priority=priority, disclosure=disclosure,
            domain=domain, namespace=namespace,
            world_timestamp=world_timestamp,
        )
        # 对齐 create_memory 的返回形状，调用方无须感知升级。
        result = {
            "id": upgraded.get("new_memory_id"),
            "node_uuid": upgraded.get("node_uuid"),
            "domain": domain,
            "path": path,
            "uri": f"{domain}://{path}",
            "priority": priority,
            "rows_after": upgraded.get("rows_after", {}),
        }
        return result, True


async def _ensure_target_parent(graph, domain: str, path: str, namespace: str) -> Optional[str]:
    """确保目标路径的父路径存在；不存在则自动创建容器节点。

    移动/归档到新位置时，目标父目录（如 archive://scenes/xxx 的 scenes）
    很可能还不存在。这里自动补一个轻量容器节点，返回创建的容器 path；
    父已存在时返回 None。注意：只确保「父」存在，不创建目标节点本身。
    """
    if "/" not in path:
        return None
    created = await _ensure_parent_chain(
        graph, domain, path.rsplit("/", 1)[0], namespace
    )
    return created[-1] if created else None


async def _move_memory(
    graph,
    source_uri: str,
    target_uri: str,
    namespace: str,
    dry_run: bool = False,
) -> Tuple[str, str, str]:
    """把一条记忆连同整棵子树移到新位置。

    可跨域、可改名（目标可以是完整新 URI）。顺序保证：先挂新路径
    （级联建子树路径），再删旧路径（子节点已有新路径，不会触发孤儿保护）。

    Returns:
        (new_uri, node_uuid, info) — info 是给角色看的附加说明
        （如自动创建的容器目录）。
    """
    src_domain, src_path = parse_uri(source_uri)
    tgt_domain, tgt_path = parse_uri(target_uri)
    valid = _get_valid_domain_list()
    if tgt_domain not in valid:
        raise ValueError(f"没有 '{tgt_domain}' 这个域名。可用：{', '.join(valid)}")
    if not src_path:
        raise ValueError(f"不能移动域名根 '{src_domain}://'。")
    if not tgt_path:
        raise ValueError("目标必须是完整路径（如 'archive://scenes/xxx'），不能是域名根。")
    if src_domain == tgt_domain and src_path == tgt_path:
        raise ValueError("源和目标完全相同，没有可移动的。")

    src = await graph.get_memory_by_path(src_path, src_domain, namespace=namespace)
    if not src:
        raise ValueError(f"没找到源「{source_uri}」。")
    if await graph.get_memory_by_path(tgt_path, tgt_domain, namespace=namespace):
        raise ValueError(f"目标「{target_uri}」已经存在。先 forget 掉或换个目标位置。")

    info = ""
    if not dry_run:
        created_parent = await _ensure_target_parent(graph, tgt_domain, tgt_path, namespace)
        if created_parent:
            info = f"（自动创建了目录 {tgt_domain}://{created_parent}）"

        await graph.add_path(
            new_path=tgt_path,
            target_path=src_path,
            new_domain=tgt_domain,
            target_domain=src_domain,
            priority=src.get("priority", 5),
            disclosure=src.get("disclosure"),
            namespace=namespace,
        )
        await graph.remove_path(src_path, src_domain, namespace=namespace)
    else:
        # 预览模式：只提示父目录是否需要自动创建
        if "/" in tgt_path:
            parent_path = tgt_path.rsplit("/", 1)[0]
            if not await graph.get_memory_by_path(parent_path, tgt_domain, namespace=namespace):
                info = f"（目标目录 {tgt_domain}://{parent_path} 不存在，执行时会自动创建）"

    return f"{tgt_domain}://{tgt_path}", src["node_uuid"], info


# ── 查看 ──────────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_memory(uri: str, character_id: str = "", depth: int = 0, max_nodes: int = 200) -> str:
    """查看一段记忆的内容。

    这是你回想起某件事的主要方式。输入 URI 就能看到那里的内容，包括子节点和相关的触发词关联。

    Args:
        uri: 记忆的 URI，例如 core://identity/habits
        character_id: 你的角色 ID（用于记忆隔离），如 "player"/"elena"/"world"。
                      留空则使用进程默认 namespace。
        depth: 展开子树的层数。
            0（默认）= 只显示本节点全文 + 直接子节点的 URI 列表（原行为，完全兼容）。
            1 = 本节点 + 直接子节点全文。
            N = 递归 N 层。
            -1 = 展开整棵子树。
        max_nodes: 子树模式下最多渲染多少条节点的正文（防止一次读取刷爆上下文）。
            到达上限后，剩余节点只列出 URI 并标注「内容省略」。

        特殊系统视图（不需要记忆也看得到）：
        - system://boot        : 醒来时最先看到的记忆
        - system://wakeup/<N>  : boot 全文 + 最近动态 + 最近 N 个场景
        - system://index/<domain>: 查看某个域下的所有记忆索引（如 system://index/core）
        - system://recent/<N>  : 查看最近修改的 N 条记忆（如 system://recent/10）
        - system://timeline/<domain>/<N>: 按世界时间（故事时间）倒序的事件轴，
          如 system://timeline/history/10 或 system://timeline/2020-09-01（某日起全部）
        - system://forgotten/<domain>/<N>: 沉睡最久的记忆——最久没想起的 N 条，
          用来主动回顾快要遗忘的东西
        - system://glossary    : 所有触发词索引
    """
    try:
        stripped = uri.strip()

        async def _do():
            # ── System URI handling ────────────────────────────────────────
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

                elif cmd == "timeline":
                    # system://timeline[/domain][/N|YYYY-MM-DD]
                    # A bare date segment filters entries from that date on;
                    # a digit-only segment is the entry limit.
                    domain_arg = parts[1] if len(parts) > 1 else None
                    extra = parts[2] if len(parts) > 2 else None
                    limit = 10
                    since = None
                    if extra:
                        if extra.isdigit():
                            limit = int(extra)
                        else:
                            since = extra
                    if domain_arg and "-" in domain_arg:
                        # system://timeline/2020-09-01 — date without domain
                        since = domain_arg
                        domain_arg = None
                    return await generate_timeline_view(
                        domain=domain_arg, limit=limit, since=since
                    )

                elif cmd == "forgotten":
                    domain_arg = parts[1] if len(parts) > 1 else None
                    limit = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 10
                    return await generate_forgotten_view(
                        domain=domain_arg, limit=limit
                    )

                elif cmd == "glossary":
                    return await generate_glossary_index_view()

                elif cmd == "diagnostic":
                    domain = parts[1] if len(parts) > 1 else DEFAULT_DOMAIN
                    days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 30
                    return await generate_diagnostic_view(domain, days)

                else:
                    return f"未知的系统视图：{stripped}。试试 system://boot, system://wakeup, system://index/<domain>, system://recent/<N>, system://timeline/<domain>/<N>, system://forgotten/<domain>/<N>, system://glossary, system://diagnostic/<domain>"

            # ── Normal memory lookup ───────────────────────────────────────
            return await fetch_and_format_memory(
                stripped, track_access=True, depth=depth, max_nodes=max_nodes
            )

        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()
    except ValueError as e:
        return f"出错了：{str(e)}"
    except Exception as e:
        return f"出错了：{str(e)}"


@mcp.tool()
async def search_memory(query: str, domain: Optional[str] = None, limit: int = 10, sort_by_world: bool = False, semantic: bool = False, character_id: str = "") -> str:
    """搜索记忆。想不起 URI 的时候用这个来找。

    默认是全文搜索（词法），输入关键词就能找到相关记忆。
    semantic=True 时启用语义检索（需要配置 embedding API），能召回关键词不重叠但语义相关的记忆；未配置 embedding API 时自动退化为词法搜索。

    Args:
        query: 搜索关键词
        domain: 可选，限定在某个域名下搜索（如 "core"、"history"）
        limit: 最多返回多少条（默认 10）
        sort_by_world: 是否按世界时间排序（默认按现实时间）
        semantic: 是否启用语义检索（向量召回 + 词法融合；需配置 embedding API）
        character_id: 你的角色 ID（用于记忆隔离），如 "player"/"elena"/"world"。留空用默认 namespace。
    """
    graph = get_graph_service()

    try:
        from mcp_server import get_valid_domains
        valid = get_valid_domains()
        if domain is not None and domain not in valid:
            return f"没有 '{domain}' 这个域名。可用的：{', '.join(valid)}"

        async def _search():
            return await graph.search_memories(
                query, domain, limit=limit, namespace=get_namespace(), semantic=semantic
            )

        if character_id:
            async with namespace_scope(character_id):
                results = await _search()
        else:
            results = await _search()

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
async def remember_memory(uri: str, content: str, time: Optional[str] = None, character_id: str = "") -> str:
    """记下一段新的记忆。

    Args:
        uri: 记忆的路径（URI），例如 core://identity
        content: 记忆的具体内容
        time: 可选。事件发生的世界时间（YYYY-MM-DD）。
              支持相对位移，如 "-1d"（昨天）, "+1y"（明年）。
              【何时使用时间？】
              - Events：需要时间线追踪的具体事件必须写明时间。例如某次相遇、交流（如 core://events/first_impression）。
              - Static：背景故事、性格习惯、世界观规则、常识（如 core://identity, core://world, core://relationships）。这类信息是永久有效的，无需传入时间。
        character_id: 你的角色 ID（用于记忆隔离），如 "player"/"elena"/"world"。留空用默认 namespace。

    Note:
        父节点不存在时不会报错：会先用「（记得补充）」占位补齐缺失的祖先，
        子节点照常写入，返回结果里会提醒你稍后回写父节点的真实内容。
    """
    try:
        async def _do():
            # Split URI into domain, parent_path, and title
            domain, full_path = parse_uri(uri)
            if "/" in full_path:
                parent_path, title = full_path.rsplit("/", 1)
            else:
                parent_path = ""
                title = full_path

            # Handle world time parsing (real-clock mode when world clock disabled)
            final_world_time = None
            config = get_config()
            clock = config.get("world_clock", {})
            _, current_world_time = _cfg.get_clock_state()

            if time:
                from system_views import parse_relative_offset
                # Try parsing as offset first
                offset_date = parse_relative_offset(time, current_world_time)
                final_world_time = offset_date or time
            elif clock.get("auto_timestamp") and current_world_time:
                final_world_time = current_world_time

            graph = get_graph_service()
            # 父节点不存在时不再报错：先补占位父链，让子节点立刻落地，
            # 结果里提醒模型稍后回写这些占位节点的真实内容。
            placeholders = await _ensure_parent_chain(
                graph, domain, parent_path, get_namespace()
            )

            result, upgraded = await _create_or_upgrade_memory(
                graph, domain, full_path, content,
                priority=5, namespace=get_namespace(),
                world_timestamp=final_world_time,
            )

            msg = f"已记下记忆: {result['uri']}" + (f" (发生于 {final_world_time})" if final_world_time else "")
            if upgraded:
                msg += "\n（并发写入时这条路径先被占位占住了，已把你的真实内容升级进去。）"
            msg += _format_placeholder_notice(domain, placeholders)
            return msg


        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()
    except Exception as e:
        return f"记录失败: {str(e)}"


@mcp.tool()
async def set_world_time(time: str, character_id: str = "") -> str:
    """设置当前世界时间（按 namespace 隔离）。

    改变指定 namespace 的世界时间后，该 namespace 后续创建的记忆会自动关联到新时间，
    且在查看记忆时会更新“N天前”的计算参考。GM 可通过 character_id 指定要调整的世界
    （例如 character_id="elias" 调整 magnolia 世界时钟，不影响其他 namespace）。

    Args:
        time: 世界观日期（如 2024-06-05）或相对偏移量（如 "+1d"）。
        character_id: 目标角色 ID / namespace（用于世界时钟隔离）。留空用当前/默认 namespace。
    """
    try:
        async def _do():
            ns = get_namespace()
            clock = dict(_cfg.get_world_clock(ns))
            current_time = clock.get("current_time", "2024-06-01")

            from system_views import parse_relative_offset
            new_time = parse_relative_offset(time, current_time) or time

            clock["current_time"] = new_time
            _cfg.set_world_clock(clock, ns)

            return f"当前世界时间已设置为: {new_time}"
        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()
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
    character_id: str = "",
) -> ToolResult | CreateResult:
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
        character_id: 你的角色 ID（用于记忆隔离），如 "player"/"elena"/"world"。留空用默认 namespace。

    Returns:
        新建记忆的 URI

    Note:
        父节点不存在时不会报错：会先用「（记得补充）」占位补齐缺失的祖先，
        子节点照常写入，返回结果里会提醒你稍后回写父节点的真实内容。
    """
    graph = get_graph_service()

    try:
        if not when or not when.strip():
            return ToolResult(message="每条记忆都需要一个「什么时候会想起」的条件(when)。不写的话这条记忆就永远找不到了。")

        if title:
            if not re.match(r"^[a-zA-Z0-9_-]+$", title):
                return ToolResult(message="标题只能包含字母、数字、连字符和下划线（不能有空格、斜杠、特殊字符）。")

        async def _do():
            domain, parent_path = parse_uri(parent_uri)

            # --- 时间解析与隐式继承逻辑 ---
            final_world_time = None
            config = get_config()
            clock = config.get("world_clock", {})
            _, current_world_time = _cfg.get_clock_state()

            # 父节点不存在时不再报错：先补占位父链，让子节点立刻落地，
            # 结果里提醒模型稍后回写这些占位节点的真实内容。
            placeholders = await _ensure_parent_chain(
                graph, domain, parent_path, get_namespace()
            )

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

            if title:
                # 具名子节点可能撞上并发占位（先写了它的子孙再回头写它），
                # 走升级 helper；自动编号路径不会撞占位，直接建。
                result, upgraded = await _create_or_upgrade_memory(
                    graph, domain,
                    f"{parent_path}/{title}" if parent_path else title,
                    content, priority=importance, disclosure=when,
                    namespace=get_namespace(),
                    world_timestamp=final_world_time,
                )
            else:
                result = await graph.create_memory(
                    parent_path=parent_path,
                    content=content,
                    priority=importance,
                    title=None,
                    disclosure=when,
                    domain=domain,
                    namespace=get_namespace(),
                    world_timestamp=final_world_time, # 注入时间
                )
                upgraded = False

            created_uri = result.get("uri", make_uri(domain, result["path"]))
            _record_rows(before_state={}, after_state=result.get("rows_after", {}))

            db = get_db_manager()
            async with db.session() as session:
                rev_id = await commit_checkpoint(session)

            msg = f"记住了：「{created_uri}」"
            if final_world_time:
                msg += f" (发生于 {final_world_time})"
            if upgraded:
                msg += "\n（这条路径此前是占位节点，已用你的内容升级，不必再回写它。）"

            msg += _format_placeholder_notice(domain, placeholders)

            if result.get("path"):
                msg += f"\n\n新记的事已经放好了。你看看和它相关的其他记忆有没有什么要整理的？"
            return CreateResult(
                message=msg,
                revision_id=rev_id,
                node_uuid=result["node_uuid"],
                uri=created_uri,
            )

            created_uri = result.get("uri", make_uri(domain, result["path"]))
            _record_rows(before_state={}, after_state=result.get("rows_after", {}))

            db = get_db_manager()
            async with db.session() as session:
                rev_id = await commit_checkpoint(session)

            msg = f"记住了：「{created_uri}」"
            if final_world_time:
                msg += f" (发生于 {final_world_time})"

            msg += _format_placeholder_notice(domain, placeholders)

            if result.get("path"):
                msg += f"\n\n新记的事已经放好了。你看看和它相关的其他记忆有没有什么要整理的？"
            return CreateResult(
                message=msg,
                revision_id=rev_id,
                node_uuid=result["node_uuid"],
                uri=created_uri,
            )

        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()

    except ValueError as e:
        return ToolResult(message=f"没记住：{str(e)}")
    except Exception as e:
        return ToolResult(message=f"没记住：{str(e)}")


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
    character_id: str = "",
) -> ToolResult | UpdateResult:
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
        time: 可选，修改该记忆发生的世界时间（如 "2024-06-01" 或 "-1d"）；传 "" 清除时间
    Examples:
        edit_memory("core://identity/habits", old_text="每天喝咖啡", new_text="每天喝茶")
        edit_memory("core://events/encounter_0302", append="\\n今天（3月3日）又遇到了他……")
        edit_memory("diary://0521_special_day", line=3, line_content="新的第三行内容")
        edit_memory("core://schedule", importance=2)  # 只改重要性
    """
    graph = get_graph_service()

    # Handle world time parsing: time="" clears the stored world time
    # (matching the new_text="" delete-text convention).
    final_world_time = None
    clear_time = False
    if time is not None:
        if time == "":
            clear_time = True
        else:
            _, current_world_time = _cfg.get_clock_state()
            from system_views import parse_relative_offset
            offset_date = parse_relative_offset(time, current_world_time)
            final_world_time = offset_date or time


    try:
        async def _do():
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
                return ToolResult(message=f"没找到「{full_uri}」这条记忆。")

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
                    return ToolResult(message=f"行号 {line} 超出范围。这个记忆一共有 {len(lines)} 行。")
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
                clear_world_timestamp=clear_time,
            )

            _record_rows(
                before_state=result.get("rows_before", {}),
                after_state=result.get("rows_after", {}),
            )

            db = get_db_manager()
            async with db.session() as session:
                rev_id = await commit_checkpoint(session)

            msg = f"已经改好了：「{full_uri}」"
            if clear_time:
                msg += " (时间已清除)"
            elif final_world_time:
                msg += f" (时间更新为: {final_world_time})"
            return UpdateResult(
                message=msg,
                revision_id=rev_id,
                node_uuid=result["node_uuid"],
                uri=full_uri,
                old_memory_id=result.get("old_memory_id"),
                new_memory_id=result.get("new_memory_id"),
            )
        
        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()

    except ValueError as e:
        return ToolResult(message=f"没改掉：{str(e)}")
    except Exception as e:
        return ToolResult(message=f"没改掉：{str(e)}")


# ── 删除 ──────────────────────────────────────────────────────────────────

@write_tool()
async def forget_memory(uri: str, character_id: str = "") -> ToolResult | ForgetResult:
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
        async def _do():
            domain, path = parse_uri(uri)
            full_uri = make_uri(domain, path)

            memory = await graph.get_memory_by_path(path, domain, namespace=get_namespace())
            if not memory:
                return ToolResult(message=f"没找到「{full_uri}」这条记忆。")

            result = await graph.remove_path(path, domain, namespace=get_namespace())
            rows_before = result.get("rows_before", {})

            _record_rows(
                before_state=rows_before,
                after_state={},
            )

            deleted_path_count = len(rows_before.get("paths", []))
            descendant_count = max(0, deleted_path_count - 1)
            msg = f"忘掉了：「{full_uri}」"
            if descendant_count > 0:
                msg += f"（连带清掉了 {descendant_count} 个子节点）"

            return msg
        
        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()

    except ValueError as e:
        return ToolResult(message=f"没忘掉：{str(e)}")
    except Exception as e:
        return ToolResult(message=f"没忘掉：{str(e)}")


# ── 关联 ──────────────────────────────────────────────────────────────────

@write_tool()
async def link_memory(
    target_uri: str,
    new_uri: str,
    importance: int,
    when: str,
    character_id: str = "",
) -> ToolResult | LinkResult:
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
        async def _do():
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

            db = get_db_manager()
            async with db.session() as session:
                rev_id = await commit_checkpoint(session)

            return LinkResult(
                message=msg,
                revision_id=rev_id,
                target_uri=target_uri,
                new_uri=alias_uri,
            )
        
        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()

    except ValueError as e:
        return ToolResult(message=f"没加上：{str(e)}")
    except Exception as e:
        return ToolResult(message=f"没加上：{str(e)}")


@write_tool()
async def tag_memory(
    uri: str,
    add: Optional[List[str]] = None,
    remove: Optional[List[str]] = None,
    character_id: str = "",
) -> ToolResult | TagResult:
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
        async def _do():
            domain, path = parse_uri(uri)
            full_uri = make_uri(domain, path)

            memory = await graph.get_memory_by_path(path, domain, namespace=get_namespace())
            if not memory:
                return ToolResult(message=f"没找到「{full_uri}」。")

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

            db = get_db_manager()
            async with db.session() as session:
                rev_id = await commit_checkpoint(session)

            return TagResult(
                message="\n".join(lines),
                revision_id=rev_id,
                node_uuid=node_uuid,
                added=added,
                removed=removed,
            )
        
        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()

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
    character_id: str = "",
) -> ToolResult | CreateResult:
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

    Note:
        目标父节点不存在时不会报错：会先用「（记得补充）」占位补齐，
        合并结果照常写入，返回消息里会提醒你回写父节点。
        源记忆缺失或删除失败都会在返回消息里列出来，不会静默跳过。
    """
    graph = get_graph_service()
    glossary = get_glossary_service()

    try:
        async def _do():
            if len(uris) < 2:
                return ToolResult(message="至少需要两条记忆才能合并。")

            target_domain, target_path = parse_uri(target_uri)
            namespace = get_namespace()

            # 1. 读取所有源记忆
            #    缺失的一次性全列出来，而不是撞到第一条就退出——模型需要
            #    一眼看清要补哪几条，而不是来回试。
            sources = []
            source_glossary_keywords = []
            missing = []
            for uri in uris:
                domain, path = parse_uri(uri)
                memory = await graph.get_memory_by_path(path, domain, namespace=namespace)
                if not memory:
                    missing.append(uri)
                    continue
                sources.append((domain, path, memory))
                # 收集标签
                node_glossary = await glossary.get_glossary_for_node(memory["node_uuid"], namespace=namespace)
                source_glossary_keywords.extend(node_glossary)

            if missing:
                return ToolResult(
                    message=f"没找到源记忆：{'、'.join(missing)}。请确认 URI 后再合并。"
                )
            if len(sources) < 2:
                return ToolResult(message="至少需要两条记忆才能合并。")

            # 2. 创建目标记忆
            parent_path = "/".join(target_path.split("/")[:-1])
            title_part = target_path.split("/")[-1]

            # 父节点不存在时先补占位父链，让合并结果立刻落地。
            placeholders = await _ensure_parent_chain(
                graph, target_domain, parent_path, namespace
            )

            result, upgraded = await _create_or_upgrade_memory(
                graph, target_domain, target_path, content,
                priority=3, disclosure="当需要回想合并后的事时",
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
            #    失败要报出来：源留在原处会让模型以为合并很干净，
            #    实际上旧入口还在，之后会重复想起同一件事。
            deleted_sources = []
            failed_sources = []
            for domain, path, memory in sources:
                full_uri = make_uri(domain, path)
                try:
                    await graph.remove_path(path, domain, namespace=namespace)
                    deleted_sources.append(full_uri)
                except Exception as e:
                    failed_sources.append(f"{full_uri}（{e}）")

            _record_rows(
                before_state=result.get("rows_before", {}),
                after_state=result.get("rows_after", {}),
            )

            msg = f"合并完成：{len(sources)} 条记忆 → 「{created_uri}」"
            if reason:
                msg += f"\n原因：{reason}"
            msg += f"\n已删除旧入口：{len(deleted_sources)}/{len(sources)} 条"
            if failed_sources:
                msg += f"\n没删掉的旧入口（还留在原处，需要手动确认）：{'、'.join(failed_sources)}"
            if source_glossary_keywords:
                transferred = len(set(source_glossary_keywords))
                msg += f"\n转移了 {transferred} 个标签到新记忆"
            if upgraded:
                msg += "\n（目标路径此前是占位节点，已用合并内容升级，不必再回写它。）"
            msg += _format_placeholder_notice(target_domain, placeholders)

            db = get_db_manager()
            async with db.session() as session:
                rev_id = await commit_checkpoint(session)

            return CreateResult(
                message=msg,
                revision_id=rev_id,
                node_uuid=target_node_uuid or "",
                uri=created_uri,
            )
        
        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()

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
    character_id: str = "",
) -> ToolResult | CreateResult:
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

    Note:
        主题父节点不存在时不会报错：会先用「（记得补充）」占位补齐，
        主题照常建立，返回消息里会提醒你回写父节点。
        源记忆关联不上、旧入口删不掉都会在返回消息里列出来，不会静默跳过。
    """
    graph = get_graph_service()
    glossary = get_glossary_service()

    try:
        async def _do():
            if not source_uris:
                return ToolResult(message="至少需要一条源记忆来整理。")

            if mode not in ("move", "link", "keep"):
                return ToolResult(message="mode 必须是 move、link 或 keep。")

            target_domain, target_path = parse_uri(target_uri)
            namespace = get_namespace()

            # 确定主题的父路径和标题
            parent_path = "/".join(target_path.split("/")[:-1]) if "/" in target_path else ""
            title_part = target_path.split("/")[-1]

            # 1. 创建主题总结节点
            #    父节点不存在时先补占位父链：主题先落地，源记忆才有地方挂。
            placeholders = await _ensure_parent_chain(
                graph, target_domain, parent_path, namespace
            )

            result, upgraded = await _create_or_upgrade_memory(
                graph, target_domain, target_path, content,
                priority=importance,
                disclosure=when or f"当说到{title_part}时",
                namespace=namespace,
            )

            target_node_uuid = result.get("node_uuid")
            topic_uri = result.get("uri", make_uri(target_domain, result["path"]))

            if tags and target_node_uuid:
                for kw in tags:
                    kw = kw.strip()
                    if kw:
                        try:
                            await glossary.add_glossary_keyword(kw, target_node_uuid, namespace=namespace)
                        except ValueError:
                            pass

            # 3. 处理源记忆
            #    每一条的成败都要记下来。以前这里整段 try/except: pass，
            #    挂不上就静默跳过，模型拿到「整理好了」却一条源都没归拢。
            linked = 0
            moved = 0
            link_failures = []
            move_failures = []
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
                except Exception as e:
                    link_failures.append(f"{src_uri} → {target_domain}://{child_path}（{e}）")
                    continue

                if mode == "move":
                    try:
                        await graph.remove_path(src_path, src_domain, namespace=namespace)
                        moved += 1
                    except Exception as e:
                        move_failures.append(f"{src_uri}（{e}）")

            _record_rows(
                before_state={},
                after_state=result.get("rows_after", {}),
            )

            total = len(source_uris)
            msg_parts = [f"整理好了：「{topic_uri}」"]
            if mode == "keep":
                msg_parts.append("  模式 keep：只建了主题，没动源记忆")
            else:
                msg_parts.append(f"  关联了 {linked}/{total} 条记忆到主题下")
                if link_failures:
                    msg_parts.append(
                        f"  没关联上的：{'、'.join(link_failures)}"
                    )
                if mode == "move":
                    msg_parts.append(f"  移除了 {moved}/{linked} 个旧入口")
                    if move_failures:
                        msg_parts.append(
                            f"  没删掉的旧入口（还留在原处）：{'、'.join(move_failures)}"
                        )
            if tags:
                msg_parts.append(f"  标签：{', '.join(tags)}")

            if upgraded:
                msg_parts.append("  主题路径此前是占位节点，已用本次内容升级，不必再回写它。")
            notice = _format_placeholder_notice(target_domain, placeholders)
            if notice:
                msg_parts.append(notice.lstrip("\n"))

            db = get_db_manager()
            async with db.session() as session:
                rev_id = await commit_checkpoint(session)

            return CreateResult(
                message="\n".join(msg_parts),
                revision_id=rev_id,
                node_uuid=target_node_uuid or "",
                uri=topic_uri,
            )
        
        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()

    except ValueError as e:
        return ToolResult(message=f"没整理好：{str(e)}")
    except Exception as e:
        return ToolResult(message=f"没整理好：{str(e)}")


# ── 移动 / 重命名 ──────────────────────────────────────────────────────────

@write_tool()
async def rename_memory(
    uri: str,
    new_title: str,
    character_id: str = "",
) -> ToolResult | UpdateResult:
    """给一段记忆改名字（路径最后一段）。内容和子节点都会跟着搬，标签不动。

    改名是「移动」的特例：只改名字、不换位置。如果还想同时换到别的
    域或目录下，用 move_memory。

    Args:
        uri: 要改名的记忆 URI，如 "core://events/luckin_0922"
        new_title: 新名字（路径最后一段）。只能用字母、数字、连字符和下划线。
        character_id: 你的角色 ID（用于记忆隔离），如 "player"/"elena"/"world"。留空用默认 namespace。

    Examples:
        rename_memory("core://events/luckin_0922", "luckin_first_sight_0922")
    """
    if not new_title or not re.match(r"^[a-zA-Z0-9_-]+$", new_title):
        return ToolResult(message="新名字只能包含字母、数字、连字符和下划线（不能有空格、斜杠）。")

    graph = get_graph_service()

    try:
        async def _do():
            domain, path = parse_uri(uri)
            if not path:
                return ToolResult(message="不能重命名域名根。")

            parent_path = path.rsplit("/", 1)[0] if "/" in path else ""
            target_uri = make_uri(domain, f"{parent_path}/{new_title}" if parent_path else new_title)

            new_uri, node_uuid, info = await _move_memory(
                graph, uri, target_uri, get_namespace()
            )
            db = get_db_manager()
            async with db.session() as session:
                rev_id = await commit_checkpoint(session)

            msg = f"改名完成：「{uri}」→「{new_uri}」"
            if info:
                msg += f"\n{info}"
            return UpdateResult(
                message=msg,
                revision_id=rev_id,
                node_uuid=node_uuid,
                uri=new_uri,
            )

        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()

    except ValueError as e:
        return ToolResult(message=f"没改名：{str(e)}")
    except Exception as e:
        return ToolResult(message=f"没改名：{str(e)}")


@write_tool()
async def move_memory(
    uri: str,
    target_uri: str,
    character_id: str = "",
) -> ToolResult | UpdateResult:
    """把一段记忆（连同子节点）搬到新位置。可以跨域、可以改名。

    适合整理时把节点从一处迁到另一处——比如把退役场景从 history 域
    归档到 archive 域，或者把零散碎片归到主题下。目标目录不存在时
    会自动创建。

    Args:
        uri: 要移动的记忆 URI，如 "history://scenes/warm_water_aftermath_0908_1836"
        target_uri: 目标位置的完整 URI，如 "archive://scenes/warm_water_0908"
        character_id: 你的角色 ID（用于记忆隔离），如 "player"/"elena"/"world"。留空用默认 namespace。

    Examples:
        move_memory("history://scenes/warm_water_aftermath_0908_1836", "archive://scenes/warm_water_0908")
    """
    graph = get_graph_service()

    try:
        async def _do():
            new_uri, node_uuid, info = await _move_memory(
                graph, uri, target_uri, get_namespace()
            )
            db = get_db_manager()
            async with db.session() as session:
                rev_id = await commit_checkpoint(session)

            msg = f"移动完成：「{uri}」→「{new_uri}」"
            if info:
                msg += f"\n{info}"
            return UpdateResult(
                message=msg,
                revision_id=rev_id,
                node_uuid=node_uuid,
                uri=new_uri,
            )

        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()

    except ValueError as e:
        return ToolResult(message=f"没移动：{str(e)}")
    except Exception as e:
        return ToolResult(message=f"没移动：{str(e)}")


# ── 批量操作 ───────────────────────────────────────────────────────────────

def _format_batch_report(
    action: str,
    items: List[Tuple[str, str, str]],
    dry_run: bool,
) -> str:
    """把批量结果列表格式化成给角色看的报告。

    items: [(status, label, detail)]，status 为 "ok" / "fail" / "skip"。
    """
    ok = [it for it in items if it[0] == "ok"]
    failed = [it for it in items if it[0] == "fail"]
    skipped = [it for it in items if it[0] == "skip"]

    head = f"{action}预览（未落库）：" if dry_run else f"{action}完成："
    lines = [head, ""]
    for status, label, detail in items:
        mark = {"ok": "[OK]", "fail": "[失败]", "skip": "[跳过]"}.get(status, "[跳过]")
        lines.append(f"- {mark} {label}")
        if detail:
            lines.append(f"    {detail}")
    lines.append("")
    lines.append(
        f"成功 {len(ok)} 条，跳过 {len(skipped)} 条，失败 {len(failed)} 条。"
    )
    return "\n".join(lines)


@write_tool()
async def batch_move_memories(
    moves: List[Dict[str, str]],
    dry_run: bool = False,
    character_id: str = "",
) -> ToolResult:
    """批量移动记忆。把一组记忆各自搬到新位置（可跨域、可改名）。

    适合整批整理：把一批 history_raw 归档到 archive 域、把一批场景
    收敛到 scene 域等。每条独立执行，单条失败不阻断其他条。
    目标目录不存在时会自动创建。

    Args:
        moves: 移动清单，每个元素 {"source_uri": "从哪里", "target_uri": "到哪里"}。
        dry_run: True 时只预览（检查源/目标/冲突），不真正执行。
        character_id: 你的角色 ID（用于记忆隔离）。留空用默认 namespace。

    Examples:
        batch_move_memories([
            {"source_uri": "history_raw://scenes/a_raw", "target_uri": "archive://scenes/a_raw"},
            {"source_uri": "history://scenes/b", "target_uri": "archive://scenes/b"},
        ])
    """
    if not moves:
        return ToolResult(message="moves 不能为空。")

    graph = get_graph_service()

    try:
        async def _do():
            namespace = get_namespace()
            results: List[Tuple[str, str, str]] = []
            for m in moves:
                src = m.get("source_uri", "") if isinstance(m, dict) else ""
                tgt = m.get("target_uri", "") if isinstance(m, dict) else ""
                label = f"{src} → {tgt}"
                try:
                    new_uri, _, info = await _move_memory(graph, src, tgt, namespace, dry_run=dry_run)
                    detail = f"将变为 {new_uri}" if dry_run else f"已是 {new_uri}"
                    if info:
                        detail += f" {info}"
                    results.append(("ok", label, detail))
                except ValueError as e:
                    results.append(("fail", label, str(e)))
                except Exception as e:
                    results.append(("fail", label, str(e)))

            rev_id = None
            if not dry_run and any(r[0] == "ok" for r in results):
                db = get_db_manager()
                async with db.session() as session:
                    rev_id = await commit_checkpoint(session)

            return ToolResult(
                message=_format_batch_report("批量移动", results, dry_run),
                revision_id=rev_id,
            )

        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()

    except Exception as e:
        return ToolResult(message=f"批量移动没跑完：{str(e)}")


@write_tool()
async def batch_forget_memories(
    uris: List[str],
    dry_run: bool = False,
    character_id: str = "",
) -> ToolResult:
    """批量忘掉记忆。一组 URI 一次清理，带预览和失败隔离。

    适合清创：一批退役节点、重复节点一次删掉。删除前有防呆——
    带子节点的会连坐，被同批更靠前的删除覆盖的会自动跳过；
    单条失败不阻断其他条。

    Args:
        uris: 要删除的 URI 列表。
        dry_run: True 时只预览（每条是否存在、会连坐哪些子节点），不真正执行。
        character_id: 你的角色 ID（用于记忆隔离）。留空用默认 namespace。

    Examples:
        batch_forget_memories([
            "core://events/luckin_0922",
            "core://events/luckin_friday_plan_0922",
        ], dry_run=True)
    """
    if not uris:
        return ToolResult(message="uris 不能为空。")

    graph = get_graph_service()

    try:
        async def _do():
            namespace = get_namespace()
            results: List[Tuple[str, str, str]] = []
            covered: List[str] = []  # 已被同批更靠前删除覆盖的 (domain, path)

            # 深度降序：叶子先删、父节点后删，避免孤儿保护误报
            # （remove_path 会拒绝删除仍有可达子节点的路径）。
            ordered_uris = sorted(
                uris,
                key=lambda u: -parse_uri(u)[1].count("/"),
            )

            for uri in ordered_uris:
                domain, path = parse_uri(uri)
                try:
                    if not path:
                        raise ValueError("不能删除域名根。")
                    # 已被前面某条的子树删除覆盖？
                    if any(
                        d == domain and (p == path or path.startswith(p + "/"))
                        for d, p in covered
                    ):
                        results.append(("skip", uri, "已被同批中更靠前的删除覆盖"))
                        continue

                    mem = await graph.get_memory_by_path(path, domain, namespace=namespace)
                    if not mem:
                        results.append(("fail", uri, "没找到这条记忆"))
                        continue

                    if dry_run:
                        children = await graph.get_children(
                            mem["node_uuid"],
                            context_domain=domain,
                            context_path=path,
                            namespace=namespace,
                        )
                        detail = "将删除（含子节点）" if children else "将删除"
                        if children:
                            detail += f"：连带 {len(children)} 个子节点"
                        results.append(("ok", uri, detail))
                    else:
                        result = await graph.remove_path(path, domain, namespace=namespace)
                        rows_before = result.get("rows_before", {})
                        _record_rows(before_state=rows_before, after_state={})
                        deleted_paths = len(rows_before.get("paths", []))
                        extra = max(0, deleted_paths - 1)
                        detail = f"已删除" + (f"（连带 {extra} 个子节点）" if extra else "")
                        results.append(("ok", uri, detail))

                    covered.append((domain, path))
                except ValueError as e:
                    results.append(("fail", uri, str(e)))
                except Exception as e:
                    results.append(("fail", uri, str(e)))

            rev_id = None
            if not dry_run and any(r[0] == "ok" for r in results):
                db = get_db_manager()
                async with db.session() as session:
                    rev_id = await commit_checkpoint(session)

            return ToolResult(
                message=_format_batch_report("批量删除", results, dry_run),
                revision_id=rev_id,
            )

        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()

    except Exception as e:
        return ToolResult(message=f"批量删除没跑完：{str(e)}")


@write_tool()
async def batch_edit_memories(
    uris: List[str],
    importance: Optional[int] = None,
    when: Optional[str] = None,
    append: Optional[str] = None,
    time: Optional[str] = None,
    dry_run: bool = False,
    character_id: str = "",
) -> ToolResult:
    """批量修改一组记忆的元数据或追加内容。

    适合全库重分级（importance 0-10）、批量改想起条件（when）、
    批量补时间（time）等维护操作。注意 append 是往每条内容末尾
    追加同一段文字——只有确实想统一补注时才用。

    Args:
        uris: 要修改的 URI 列表。
        importance: 新的重要性（0=最重要，数字越大越次要）。
        when: 新的想起条件（什么时候该想起这条）。
        append: 追加到每条内容末尾的文字。
        time: 新的世界时间（YYYY-MM-DD 或相对位移如 "-1d"）；传 "" 清除。
        dry_run: True 时只预览每条当前值 → 将改为什么，不真正执行。
        character_id: 你的角色 ID（用于记忆隔离）。留空用默认 namespace。

    Examples:
        batch_edit_memories(["history://scenes/a", "history://scenes/b"], importance=6)
        batch_edit_memories(["diary://0927_night_talk"], append="\\n（后记：……）")
    """
    if not uris:
        return ToolResult(message="uris 不能为空。")
    if importance is None and when is None and append is None and time is None:
        return ToolResult(message="至少要提供一种修改：importance、when、append 或 time。")

    graph = get_graph_service()

    try:
        async def _do():
            namespace = get_namespace()

            # 时间解析（与 edit_memory 一致）：time="" 清除世界时间
            final_world_time = None
            clear_time = False
            if time is not None:
                if time == "":
                    clear_time = True
                else:
                    _, current_world_time = _cfg.get_clock_state()
                    from system_views import parse_relative_offset
                    offset_date = parse_relative_offset(time, current_world_time)
                    final_world_time = offset_date or time

            results: List[Tuple[str, str, str]] = []
            for uri in uris:
                domain, path = parse_uri(uri)
                try:
                    if not path:
                        raise ValueError("不能编辑域名根。")
                    mem = await graph.get_memory_by_path(path, domain, namespace=namespace)
                    if not mem:
                        results.append(("fail", uri, "没找到这条记忆"))
                        continue

                    new_content = None
                    if append is not None:
                        new_content = (mem.get("content") or "") + append

                    if dry_run:
                        changes = []
                        if importance is not None:
                            changes.append(f"重要性 {mem.get('priority')} → {importance}")
                        if when is not None:
                            changes.append(f"想起条件 → {when}")
                        if clear_time:
                            changes.append("世界时间 → 清除")
                        if final_world_time is not None:
                            changes.append(f"世界时间 → {final_world_time}")
                        if append is not None:
                            changes.append(f"内容末尾追加 {len(append)} 字")
                        results.append(("ok", uri, "；".join(changes) or "无变化"))
                        continue

                    result = await graph.update_memory(
                        path=path,
                        content=new_content,
                        priority=importance,
                        disclosure=when,
                        domain=domain,
                        namespace=namespace,
                        world_timestamp=final_world_time,
                        clear_world_timestamp=clear_time,
                    )
                    _record_rows(
                        before_state=result.get("rows_before", {}),
                        after_state=result.get("rows_after", {}),
                    )
                    results.append(("ok", uri, "已修改"))
                except ValueError as e:
                    results.append(("fail", uri, str(e)))
                except Exception as e:
                    results.append(("fail", uri, str(e)))

            rev_id = None
            if not dry_run and any(r[0] == "ok" for r in results):
                db = get_db_manager()
                async with db.session() as session:
                    rev_id = await commit_checkpoint(session)

            return ToolResult(
                message=_format_batch_report("批量编辑", results, dry_run),
                revision_id=rev_id,
            )

        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()

    except Exception as e:
        return ToolResult(message=f"批量编辑没跑完：{str(e)}")


# ── 存档 ──────────────────────────────────────────────────────────────────


def _resolve_content_or_path(value: Optional[str]) -> str:
    """把「内容或文件路径」解析成内容。

    以 "\\\\"（Windows UNC）或 "/"（WSL 绝对路径）开头的值按文件读取，
    其余按字面内容返回。UNC 形如 \\\\wsl.localhost\\Ubuntu\\home\\…，
    会自动转换成 WSL 本地路径 /home/…。
    """
    if value is None:
        return ""
    stripped = value.strip()
    if not stripped:
        return ""

    path = None
    if stripped.startswith("\\\\"):
        # UNC：\\wsl.localhost\Ubuntu\home\yoshix7ti\... → /home/yoshix7ti/...
        parts = [p for p in stripped.replace("\\", "/").split("/") if p]
        if parts and parts[0].lower() == "wsl.localhost":
            # parts = ['wsl.localhost', '<发行版名>', 'home', ...]
            path = "/" + "/".join(parts[2:])
    elif stripped.startswith("/"):
        path = stripped

    if path is None:
        return value

    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise ValueError(
            f"读不了归档素材文件 {path}：{e.strerror or e}"
        ) from e


def _infer_title_from_path(value: str) -> str:
    """从文件路径推断场景标题：/path/to/sunday_note_0927.md → sunday_note_0927。"""
    name = value.strip().replace("\\", "/").rstrip("/").split("/")[-1]
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return name


@write_tool()
async def archive_history(
    title: str = "",
    history: str = "",
    mode: str = "char",
    raw: Optional[str] = None,
    time: Optional[str] = None,
    character_id: str = "",
) -> ToolResult | ArchiveResult:
    """把刚才发生的事记录到历史记录（history 域）。

    每轮对话或场景结束后，用这个工具把发生了什么记到 history 域。
    之后就可以通过「system://wakeup」来回想最近发生的事。

    history 和 raw 可以直接传内容，也可以传素材文件路径——GM 的场景
    笔记可以直接投喂，不用手写内容。路径两种写法都认：
      - WSL 绝对路径：/home/yoshix7ti/world/.pi/extensions/magnolia/export/history/elias/sunday_note_0927.md
      - Windows UNC 路径：\\\\wsl.localhost\\Ubuntu\\home\\yoshix7ti\\world\\…（自动转换）
    以 "\\\\" 或 "/" 开头的值视为文件路径读取，其余按字面内容处理。
    title 留空时自动取文件名（去掉扩展名）。

    注意：这是「记录新场景」的工具，不是「归档旧记忆」的工具。
    想把已有的记忆移进 archive 域留档，请用 move_memory（如
    move_memory("core://old_thing", "archive://old_thing")）。

    Args:
        title: 场景的简短标题（如 "sunday_note_0927" 或 "tavern_brawl"）。
               只能包含字母、数字、下划线和连字符。留空时从 history 文件
               路径推断。
        history: 场景摘要内容，或摘要文件的路径。
        mode: "char"（角色视角）或 "gm"（GM视角），默认 "char"。
        raw: 原始记录内容或文件路径。可选。
        time: 可选。存档对应的世界时间（如 "2024-06-01" 或 "-1d"）。默认使用当前世界时间。
    """
    graph = get_graph_service()

    try:
        async def _do():
            import re
            namespace = get_namespace()

            resolved_history = _resolve_content_or_path(history)
            if not resolved_history.strip():
                return ToolResult(
                    message="history 不能为空。写一下刚才发生了什么，或传一个场景摘要文件的路径。"
                )

            if not title:
                resolved_title = _infer_title_from_path(history)
            else:
                resolved_title = title
            if not resolved_title or not re.match(r"^[a-zA-Z0-9_-]+$", resolved_title):
                return ToolResult(
                    message="title 必须提供，且只能包含字母、数字、连字符和下划线（如 'first_encounter'）。"
                )

            resolved_raw = _resolve_content_or_path(raw) if raw else None

            # 确保 scenes 容器存在（新 namespace 首次归档时自动创建）
            for container_domain in ("history", "history_raw"):
                if container_domain == "history_raw" and not (resolved_raw and resolved_raw.strip()):
                    continue
                existing = await graph.get_memory_by_path(
                    "scenes", container_domain, namespace=namespace
                )
                if not existing:
                    await graph.create_memory(
                        parent_path="",
                        content="",
                        priority=8,
                        title="scenes",
                        disclosure="",
                        domain=container_domain,
                        namespace=namespace,
                    )

            # --- 世界时间处理 ---
            config = get_config()
            clock = config.get("world_clock", {})
            _, current_world_time = _cfg.get_clock_state()

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
                content=resolved_history,
                priority=5,
                title=resolved_title,
                disclosure="当回顾最近经历时",
                domain="history",
                namespace=namespace,
                world_timestamp=final_world_time, 
            )

            if resolved_raw and resolved_raw.strip():
                await graph.create_memory(
                    parent_path="scenes",
                    content=resolved_raw,
                    priority=5,
                    title=f"{resolved_title}_raw",
                    disclosure="",
                    domain="history_raw",
                    namespace=namespace,
                    world_timestamp=final_world_time, 
                )

            msg = f"场景已存档（{mode}）：history://scenes/{resolved_title}"
            if final_world_time:
                msg += f" (世界时间: {final_world_time})"
            
            db = get_db_manager()
            async with db.session() as session:
                rev_id = await commit_checkpoint(session)

            return ArchiveResult(
                message=msg,
                revision_id=rev_id,
                uri=f"history://scenes/{resolved_title}",
            )
        
        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()

    except ValueError as e:
        return f"存档失败：{str(e)}"
    except Exception as e:
        return f"存档失败：{str(e)}"

# ── 回顾 ──────────────────────────────────────────────────────────────────

@mcp.tool()
async def recent_memories(limit: int = 10, character_id: str = "") -> str:
    """看看最近发生了什么--最近改过的记忆。

    列出最近新增或修改的记忆，按时间倒序。
    可以用来快速回顾最近你在想什么、记了什么。

    Args:
        limit: 最多显示多少条（默认 10，最多 50）
        character_id: 你的角色 ID（用于记忆隔离），如 "player"/"elena"/"world"。留空用默认 namespace。

    Examples:
        recent_memories()           # 最近 10 条
        recent_memories(20)          # 最近 20 条
    """
    try:
        if character_id:
            async with namespace_scope(character_id):
                return await generate_recent_memories_view(limit)
        return await generate_recent_memories_view(limit)
    except Exception as e:
        return f"获取最近记忆失败：{str(e)}"


@write_tool()
async def boot_memory(
    action: str,
    uris: Optional[List[str]] = None,
    character_id: str = "",
) -> str:
    """管理「醒来记忆」——你醒来时最先想起的事。

    「醒来记忆」是你每次重新进入世界时最先看到的记忆，
    相当于你放在床头的东西。无需回忆就能记得清楚。
    如果你不想每次都得仔细回忆才能记起来，
    就把它放在醒来记忆里。

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
        async def _do():
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
        
        if character_id:
            async with namespace_scope(character_id):
                return await _do()
        return await _do()

    except ValueError as e:
        return f"没改掉：{str(e)}"
    except Exception as e:
        return f"没改掉：{str(e)}"


# =============================================================================
# MCP Resources
# =============================================================================


if __name__ == "__main__":
    mcp.run()