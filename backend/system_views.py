"""
System view generators for special ``system://`` URIs.

Each function produces a formatted text view from the memory graph
(boot, index, recent, glossary, diagnostic).  They are called by
``read_memory`` in ``mcp_server`` when a ``system://`` URI is requested.

Imports from ``mcp_server`` (parse_uri, make_uri, config constants) are
done inside function bodies to avoid circular imports at module level.
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from db import get_graph_service, get_glossary_service
from db.namespace import get_namespace
from locales import t
import re
from datetime import date


async def fetch_and_format_memory(uri: str, track_access: bool = False) -> str:
    """
    Fetch memory data and return a formatted string.
    Used by read_memory tool and boot view.
    """
    from mcp_server import parse_uri, make_uri, DEFAULT_DOMAIN, get_config
    graph = get_graph_service()
    glossary = get_glossary_service()
    domain, path = parse_uri(uri)

    memory = await graph.get_memory_by_path(path, domain, namespace=get_namespace())

    if not memory:
        raise ValueError(t("system.uri_not_found").format(
            uri=make_uri(domain, path)))

    if track_access and memory.get("node_uuid"):
        asyncio.create_task(
            graph.log_access(
                memory["node_uuid"],
                namespace=get_namespace(),
                context="mcp_read"
            )
        )

    children = await graph.get_children(
        memory["node_uuid"],
        context_domain=domain,
        context_path=path,
        namespace=get_namespace(),
    )

    lines = []

    disp_domain = memory.get("domain", DEFAULT_DOMAIN)
    disp_path = memory.get("path", "unknown")
    disp_uri = make_uri(disp_domain, disp_path)
    lines.append(f"# [{disp_uri}]")

    # --- World Time Context ---
    config = get_config()
    world_clock = config.get("world_clock", {})
    curr_world_time = world_clock.get("current_time")
    mem_world_time = memory.get("world_timestamp")

    if curr_world_time:
        if mem_world_time:
            rel_str = calculate_relative_world_time(mem_world_time, curr_world_time)
            if rel_str:
                lines.append(f"> (发生于: {mem_world_time}，约 {rel_str})")
            else:
                lines.append(f"> (发生于: {mem_world_time})")
        
        # Always show current anchor to help LLM context
        lines.append(f"> (当前世界时间: {curr_world_time})")

    disclosure = memory.get("disclosure")
    if disclosure:
        lines.append(f"> (想起条件: {disclosure})")

    node_keywords = await glossary.get_glossary_for_node(memory["node_uuid"], namespace=get_namespace())
    if node_keywords:
        lines.append(f"> (标签: {', '.join(node_keywords)})")

    lines.append("")
    lines.append("---")
    lines.append("")

    content = memory.get("content", "(empty)")
    lines.append(content)
    lines.append("")

    try:
        glossary_matches = await glossary.find_glossary_in_content(content, namespace=get_namespace())
        if glossary_matches:
            current_node_uuid = memory["node_uuid"]

            uri_to_keywords: Dict[str, List[str]] = {}
            for kw, nodes in glossary_matches.items():
                for n in nodes:
                    if n["node_uuid"] == current_node_uuid or n["uri"].startswith("unlinked://"):
                        continue
                    target_uri = n["uri"]
                    if target_uri not in uri_to_keywords:
                        uri_to_keywords[target_uri] = []
                    if kw not in uri_to_keywords[target_uri]:
                        uri_to_keywords[target_uri].append(kw)

            lines_to_add: List[str] = []
            if uri_to_keywords:
                for target_uri, kws in sorted(uri_to_keywords.items(), key=lambda x: (-len(x[1]), x[0])):
                    sorted_kws = sorted(kws)
                    kw_str = ", ".join(f"@{k}" for k in sorted_kws)
                    lines_to_add.append(f"- {kw_str} -> {target_uri}")

            if lines_to_add:
                lines.append("---")
                lines.append("相关联想:")
                lines.extend(lines_to_add)
                lines.append("")
    except Exception:
        pass

    if children:
        lines.append("---")
        lines.append("更深层的记忆:")
        lines.append("")

        for child in children:
            child_domain = child.get("domain", disp_domain)
            child_path = child.get("path", "")
            child_uri = make_uri(child_domain, child_path)

            child_disclosure = child.get("disclosure")
            
            if child_disclosure:
                lines.append(f"- {child_uri} ({child_disclosure})")
            else:
                lines.append(f"- {child_uri}")

    return "\n".join(lines)


async def generate_boot_memory_view(core_memory_uris: List[str]) -> str:
    """Generate the system boot memory view (system://boot)."""

    results = []
    loaded = 0
    failed = []

    for uri in core_memory_uris:
        try:
            content = await fetch_and_format_memory(uri, track_access=True)
            results.append(content)
            loaded += 1
        except Exception as e:
            failed.append(f"- {uri}: {str(e)}")

    output_parts = []
    from mcp_server import get_config
    config = get_config()
    world_clock = config.get("world_clock", {})
    curr_world_time = world_clock.get("current_time")

    output_parts.append("# 核心记忆 (Core Memories)")
    if curr_world_time:
        output_parts.append(f"> 当前世界时间: {curr_world_time}")
    output_parts.append(f"> 载入状态: {loaded}/{len(core_memory_uris)} 条记忆已浮现")
    output_parts.append("")

    if failed:
        output_parts.append("## 载入失败:")
        output_parts.extend(failed)
        output_parts.append("")

    if results:
        output_parts.append("## 记忆内容:")
        output_parts.append("")
        output_parts.append(
            "(索引建议: system://index/<domain>；近期回顾: system://recent)"
        )
        output_parts.extend(results)
    else:
        output_parts.append("(目前没有核心记忆浮现。)")

    try:
        recent_view = await generate_recent_memories_view(limit=5)
        output_parts.append("")
        output_parts.append("---")
        output_parts.append("")
        output_parts.append(recent_view)
    except Exception:
        pass

    return "\n".join(output_parts)


async def generate_memory_index_view(domain_filter: Optional[str] = None) -> str:
    """
    Generate a memory index view.

    keeps the helper usable for internal all-domain views.

    Node-centric: each conceptual entity (node_uuid) appears once per domain,
    with aliases within the same domain folded underneath its primary path.
    """
    from mcp_server import make_uri, DEFAULT_DOMAIN
    graph = get_graph_service()

    try:
        paths = await graph.get_all_paths(namespace=get_namespace())

        node_groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for item in paths:
            domain = item.get("domain", DEFAULT_DOMAIN)
            if domain_filter and domain != domain_filter:
                continue
            nid = item.get("node_uuid", "")
            node_groups.setdefault((domain, nid), []).append(item)

        entries = []
        for _key, items in node_groups.items():
            items.sort(
                key=lambda x: (
                    x["path"].count("/"),
                    x.get("priority", 0),
                    len(x["path"]),
                    x.get("uri", ""),
                )
            )
            entries.append(items[0])

        domains: Dict[str, Dict[str, list]] = {}
        for primary in entries:
            domain = primary.get("domain", DEFAULT_DOMAIN)
            domains.setdefault(domain, {})
            top_level = primary["path"].split("/")[0] if primary["path"] else "(root)"
            domains[domain].setdefault(top_level, []).append(primary)

        unique_nodes_count = len(set(nid for _, nid in node_groups.keys()))
        lines = [
            "# 记忆索引 (Memory Index)",
            f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"> 领域过滤: {domain_filter}" if domain_filter else "> 领域过滤: 全部",
            f"> 总计: {unique_nodes_count} 个概念节点",
            "",
        ]

        for domain_name in sorted(domains.keys()):
            if domain_filter and domain_name != domain_filter:
                continue
            lines.append("---")
            lines.append(f"## 领域: {domain_name}://")
            lines.append("")

            for group_name in sorted(domains[domain_name].keys()):
                lines.append(f"### {group_name}")
                for primary in sorted(
                    domains[domain_name][group_name],
                    key=lambda x: x["path"],
                ):
                    uri = primary.get("uri", make_uri(domain_name, primary["path"]))
                    priority = primary.get("priority", 0)
                    imp_str = f" [\u2605{priority}]"
                    lines.append(f"- {uri}{imp_str}")
                lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return t("system.error_index").format(error=str(e))
async def generate_recent_memories_view(limit: int = 10) -> str:
    """
    Generate a view of recently modified memories (system://recent).

    Queries non-deprecated memories ordered by created_at DESC,
    only including those that have at least one URI in the paths table.
    """
    graph = get_graph_service()

    try:
        results = await graph.get_recent_memories(limit=limit, namespace=get_namespace())

        lines = []
        lines.append("# 最近修改的记忆 (Recently Modified)")
        lines.append(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(
            f"> 显示范围: 最近 {len(results)} 条记录"
        )
        lines.append("")

        if not results:
            lines.append("(没有找到相关的记忆。)")
            return "\n".join(lines)

        for i, item in enumerate(results, 1):
            uri = item["uri"]
            priority = item.get("priority", 0)
            disclosure = item.get("disclosure")
            raw_ts = item.get("created_at", "")

            if raw_ts and len(raw_ts) >= 16:
                modified = raw_ts[:10] + " " + raw_ts[11:16]
            else:
                modified = raw_ts or "unknown"

            imp_str = f"[\u2605{priority}]"

            lines.append(f"{i}. {uri} {imp_str} (修改时间: {modified})")
            if disclosure:
                lines.append(f"   想起条件: {disclosure}")
            else:
                lines.append("   想起条件: (未设置)")
            lines.append("")
        return "\n".join(lines)

    except Exception as e:
        return t("system.error_recent").format(error=str(e))


async def generate_glossary_index_view() -> str:
    """Generate a view of all glossary keywords and their bound nodes (system://glossary)."""
    glossary = get_glossary_service()

    try:
        raw_entries = await glossary.get_all_glossary(namespace=get_namespace())

        entries = []
        for entry in raw_entries:
            valid_nodes = [
                node for node in entry.get("nodes", [])
                if not node.get("uri", "").startswith("unlinked://")
            ]
            if valid_nodes:
                entries.append({
                    "keyword": entry["keyword"],
                    "nodes": valid_nodes
                })

        lines = [
            "# 标签/触发词索引 (Glossary Index)",
            f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"> 总计: {len(entries)} 个关键词",
            "",
        ]

        if not entries:
            lines.append("(目前还没有设置任何标签。)")
            lines.append("")
            lines.append(
                "提示：可以使用 tag_memory(uri, add=['关键词']) 来为记忆添加关联词。"
            )
            return "\n".join(lines)

        for entry in entries:
            kw = entry["keyword"]
            nodes = entry["nodes"]
            lines.append(f"- {kw}")
            for node in nodes:
                lines.append(f"  -> {node['uri']}")
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return t("system.error_glossary").format(error=str(e))


async def generate_wakeup_view(boot_uris: List[str], history_limit: int = 5) -> str:
    """Generate the system wakeup view (system://wakeup)."""
    graph = get_graph_service()
    ns = get_namespace()
    sections: List[str] = []
    
    # 1. BOOT MEMORIES
    boot_blocks = []
    for uri in boot_uris:
        formatted = await _format_memory_clean(uri, ns, graph, max_children=3)
        if formatted:
            boot_blocks.append(formatted)
    if boot_blocks:
        sections.append("\n".join(boot_blocks).strip())
        
    # 2. RECENT CORE INDEX
    recent_core = await _format_recent_core_index_clean(ns, graph, limit=5)
    if recent_core:
        sections.append("## 最近动态\n" + "\n".join(recent_core))
        
    # 3. RECENT HISTORY SUMMARIES
    history_blocks = await _format_recent_domain_clean("history", ns, graph, history_limit)
    if history_blocks:
        sections.append("## 最近场景\n" + "\n---\n\n".join(history_blocks))
        
    # 4. LATEST HISTORY_RAW
    raw_blocks = await _format_recent_domain_clean("history_raw", ns, graph, 1)
    if raw_blocks:
        sections.append("## 最近场景记录\n" + "\n".join(raw_blocks))
        
    return "\n\n---\n\n".join(sections)


async def _format_memory_clean(uri: str, ns: str, graph, max_children: int = 3) -> str:
    """Format one memory in clean style: ### uri, disclosure, content, children."""
    from mcp_server import parse_uri, make_uri, DEFAULT_DOMAIN, get_config
    domain, mem_path = parse_uri(uri)
    if not domain:
        domain = DEFAULT_DOMAIN
    
    detail = await graph.get_memory_by_path(mem_path, domain, namespace=ns)
    if not detail:
        return ""
    
    content = detail.get("content", "")
    if not content:
        return ""
    
    disp_domain = detail.get("domain", domain)
    disp_path = detail.get("path", mem_path)
    disp_uri = make_uri(disp_domain, disp_path)
    disclosure = detail.get("disclosure")
    config = get_config()
    world_clock = config.get("world_clock", {})
    curr_world_time = world_clock.get("current_time")
    mem_world_time = detail.get("world_timestamp")

    lines: List[str] = []
    lines.append(f"### {disp_uri}")

    if curr_world_time:
        if mem_world_time:
            rel_str = calculate_relative_world_time(mem_world_time, curr_world_time)
            if rel_str:
                lines.append(f"> (发生于: {mem_world_time}，约 {rel_str})")
            else:
                lines.append(f"> (发生于: {mem_world_time})")

    if disclosure:
        lines.append(f"> {disclosure}")
    lines.append(content)
    lines.append("")
    
    # Children as snippets
    if max_children > 0:
        children = await graph.get_children(
            detail.get("node_uuid"),
            context_domain=domain,
            context_path=mem_path,
            namespace=ns,
        )
        if children:
            for child in children[:max_children]:
                child_domain = child.get("domain", domain)
                child_path = child.get("path", "")
                child_uri = make_uri(child_domain, child_path)
                child_disc = child.get("disclosure")
                snippet = (child.get("content_snippet") or "").replace("\n", " ").strip()
                disc_str = f" ({child_disc})" if child_disc else ""
                snip_str = f" — {snippet}" if snippet else ""
                lines.append(f"- {child_uri}{disc_str}{snip_str}")
            lines.append("")
    
    return "\n".join(lines)


async def _format_recent_domain_clean(domain: str, ns: str, graph, limit: int) -> List[str]:
    """Get recent entries from a domain, formatted clean."""
    from mcp_server import make_uri, DEFAULT_DOMAIN, get_config
    all_paths = await graph.get_all_paths(namespace=ns)
    
    # Deduplicate by node_uuid
    items = []
    seen = set()
    for item in all_paths:
        if item.get("domain", DEFAULT_DOMAIN) != domain:
            continue
        nid = item.get("node_uuid", "")
        if not nid or nid in seen:
            continue
        seen.add(nid)
        items.append(item)
    
    # Fetch details
    detailed = []
    for item in items:
        try:
            p = item.get("path", "")
            uri = item.get("uri") or make_uri(domain, p)
            detail = await graph.get_memory_by_path(p, domain, namespace=ns)
            if detail:
                detailed.append({
                    "uri": uri,
                    "path": p,
                    "content": detail.get("content", ""),
                    "disclosure": detail.get("disclosure"),
                    "created_at": detail.get("created_at", ""),
                    "world_timestamp": detail.get("world_timestamp"),
                    "node_uuid": detail.get("node_uuid"),
                })
        except Exception:
            continue
    
    # Sort by created_at DESC
    detailed.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    
    # Format output
    result = []
    for entry in detailed[:limit]:
        lines: List[str] = []
        config = get_config()
        world_clock = config.get("world_clock", {})
        curr_world_time = world_clock.get("current_time")
        mem_world_time = entry.get("world_timestamp")

        lines: List[str] = []
        lines.append(f"### {entry['uri']}")

        if curr_world_time:
            if mem_world_time:
                rel_str = calculate_relative_world_time(mem_world_time, curr_world_time)
                if rel_str:
                    lines.append(f"> (发生于: {mem_world_time}，约 {rel_str})")
                else:
                    lines.append(f"> (发生于: {mem_world_time})")

        if entry.get("disclosure"):
            lines.append(f"> {entry['disclosure']}")
        lines.append(entry["content"])
        result.append("\n".join(lines))
    
    return result


async def _format_recent_core_index_clean(ns: str, graph, limit: int = 5) -> List[str]:
    """Recent core memories as compact index lines."""
    from mcp_server import make_uri, DEFAULT_DOMAIN
    all_paths = await graph.get_all_paths(namespace=ns)
    
    # Deduplicate by node_uuid
    core_items = []
    seen = set()
    for item in all_paths:
        if item.get("domain", DEFAULT_DOMAIN) != "core":
            continue
        nid = item.get("node_uuid", "")
        if not nid or nid in seen:
            continue
        seen.add(nid)
        core_items.append(item)
    
    # Fetch details for sorting
    detailed = []
    for item in core_items:
        try:
            p = item.get("path", "")
            uri = item.get("uri") or make_uri("core", p)
            detail = await graph.get_memory_by_path(p, "core", namespace=ns)
            if detail:
                detailed.append({
                    "uri": uri,
                    "disclosure": detail.get("disclosure"),
                    "content_snippet": (detail.get("content", "") or "").replace("\n", " ").strip()[:120],
                    "created_at": detail.get("created_at", ""),
                })
        except Exception:
            continue
    
    detailed.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    
    lines: List[str] = []
    for entry in detailed[:limit]:
        disc = f" ({entry['disclosure']})" if entry.get("disclosure") else ""
        snippet = f" — {entry['content_snippet']}" if entry.get("content_snippet") else ""
        lines.append(f"- {entry['uri']}{disc}{snippet}")
    
    return lines


async def generate_memory_slot_view(slot_type: str, boot_uris: List[str] = None) -> str:
    """Generate individual sections for Pi preset slots."""
    graph = get_graph_service()
    ns = get_namespace()
    
    from mcp_server import get_config
    config = get_config()
    world_clock = config.get("world_clock", {})
    curr_world_time = world_clock.get("current_time")

    if slot_type == "boot":
        blocks = []
        if curr_world_time:
            blocks.append(f"> **当前世界时间: {curr_world_time}**")

        if boot_uris:
            for uri in boot_uris:
                formatted = await _format_memory_clean(uri, ns, graph, max_children=3)
                if formatted:
                    blocks.append(formatted)

        recent_core = await _format_recent_core_index_clean(ns, graph, limit=5)
        if recent_core:
            blocks.append("## 最近动态\n" + "\n".join(recent_core))
        
        return "\n\n---\n\n".join(blocks)

    elif slot_type == "history":
        history_blocks = await _format_recent_domain_clean("history", ns, graph, limit=5)
        if history_blocks:
            return "## 最近场景\n" + "\n---\n\n".join(history_blocks)
        return ""
        
    elif slot_type == "state":
        raw_blocks = await _format_recent_domain_clean("history_raw", ns, graph, limit=1)
        if raw_blocks:
            return "## 最近场景记录\n" + "\n".join(raw_blocks)
        return ""
        
    return f"Unknown slot type: {slot_type}"



async def generate_diagnostic_view(domain: str, days_stale: int = 30, max_children: int = 10) -> str:
    """Generate a diagnostic report of the memory graph (system://diagnostic/<domain>)."""
    graph = get_graph_service()

    try:
        priority_thresholds = {0: 3, 1: 7, 2: 14}
        diagnostics = await graph.get_diagnostics(
            namespace=get_namespace(), days_stale=days_stale, max_children=max_children, priority_thresholds=priority_thresholds, domain=domain
        )

        stale_nodes = diagnostics.get("stale_nodes", [])
        crowded_nodes = diagnostics.get("crowded_nodes", [])
        orphaned_nodes = diagnostics.get("orphaned_nodes", [])
        duplicate_aliases = diagnostics.get("duplicate_aliases", [])

        if not stale_nodes and not crowded_nodes and not orphaned_nodes and not duplicate_aliases:
            return "No issues found. Memory system is healthy."

        lines = [
            f"# Memory System Diagnostics: {domain}",
            f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            ""
        ]

        if stale_nodes:
            lines.extend([
                "## 1. Stale Memories",
                "Nodes not accessed within their priority threshold.",
                f"Thresholds: Priority 0 (<3 days), Priority 1 (<7 days), Priority 2 (<14 days), Others (<{days_stale} days).",
                ""
            ])

            sorted_stale = sorted(
                stale_nodes,
                key=lambda x: (x.get('priority') if x.get('priority') is not None else 999, -x.get('stale_days', 0))
            )

            for i, node in enumerate(sorted_stale, 1):
                last_acc = node.get("last_accessed_at")
                stale_days = node.get("stale_days")
                threshold = node.get("threshold_days", days_stale)

                if last_acc:
                    date_str = f"Last Accessed: {last_acc[:10]}"
                else:
                    date_str = "Never accessed (since tracking began)"

                lines.append(f"{i}. {node['uri']}")
                lines.append(f"   Priority: {node['priority']} | Stale for: ~{stale_days} days (Threshold: {threshold} days) | {date_str}")
            lines.append("")

        if crowded_nodes:
            lines.extend([
                "## 2. Crowded Parent Nodes",
                f"Nodes with more than {max_children} children.",
                ""
            ])
            for i, node in enumerate(crowded_nodes, 1):
                lines.append(f"{i}. {node['uri']} ({node['child_count']} children)")
            lines.append("")

        if orphaned_nodes or duplicate_aliases:
            lines.extend([
                "## 3. Anomaly Diagnostics",
                ""
            ])

            if orphaned_nodes:
                lines.extend([
                    "### 3.1 Orphaned Nodes",
                    "Nodes whose parent path no longer exists (broken path chain).",
                    "Use `read_memory` with the URI to inspect, then `add_alias` to re-parent or `delete_memory` to remove.",
                    ""
                ])
                for i, node in enumerate(orphaned_nodes, 1):
                    memory_id_str = f"Memory ID: {node['memory_id']}" if node['memory_id'] else "No active memory"
                    lines.append(f"{i}. {node['uri']}")
                    lines.append(f"   {memory_id_str} | Created: {node['created_at'][:10] if node['created_at'] else 'Unknown'}")
                    if node.get("snippet"):
                        lines.append(f"   Snippet: {node['snippet']}")
                    lines.append("")

            if duplicate_aliases:
                lines.extend([
                    "### 3.2 Duplicate Aliases under Same Parent",
                    "A single node has multiple alias paths under the same parent node.",
                    "Usually caused by accidentally inserting another alias when one already exists.",
                    "Use `delete_memory` on the redundant alias URI to remove the extra path.",
                    ""
                ])
                for i, item in enumerate(duplicate_aliases, 1):
                    parent_uri = f"{item['domain']}://{item['parent_path']}" if item['parent_path'] else f"{item['domain']}://"
                    paths_list = [f"{item['domain']}://{p}" for p in item['paths']]
                    memory_id_str = item['memory_id'] if item['memory_id'] else "No active memory"
                    lines.append(f"{i}. Memory ID: {memory_id_str}")
                    lines.append(f"   Parent: {parent_uri}")
                    lines.append(f"   Duplicate Paths ({item['count']}):")
                    for p in paths_list:
                        lines.append(f"     - {p}")
                    lines.append("")

        return "\n".join(lines).strip()

    except Exception as e:
        return t("system.error_diagnostic").format(error=str(e))


def parse_world_date(date_str: str) -> Optional[date]:
    """Parse a world date string (YYYY-MM-DD)."""
    try:
        if not date_str or not isinstance(date_str, str):
            return None
        parts = date_str.split("-")
        if len(parts) == 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        pass
    return None


def calculate_relative_world_time(target_str: str, current_str: str) -> str:
    """Calculate relative time string between two world dates."""
    target = parse_world_date(target_str)
    current = parse_world_date(current_str)

    if not target or not current:
        return ""

    diff = (target - current).days

    if diff == 0:
        return "今天"
    elif diff == -1:
        return "昨天"
    elif diff == 1:
        return "明天"
    elif diff > 0:
        return f"{diff} 天后"
    else:
        return f"{-diff} 天前"


def parse_relative_offset(offset_str: str, current_str: str) -> Optional[str]:
    """
    Parse relative offsets like '-1d', '+2m' based on current_str.
    Supports: [+/-]N[d|m|y]
    """
    current = parse_world_date(current_str)
    if not current:
        return None

    match = re.match(r"^([+-]?\d+)([dmy])$", offset_str.lower())
    if not match:
        return None

    val = int(match.group(1))
    unit = match.group(2)

    try:
        if unit == "d":
            from datetime import timedelta
            new_date = current + timedelta(days=val)
        elif unit == "m":
            # Rough month calculation
            new_month = current.month + val
            new_year = current.year + (new_month - 1) // 12
            new_month = (new_month - 1) % 12 + 1
            new_date = current.replace(year=new_year, month=new_month)
        elif unit == "y":
            new_date = current.replace(year=current.year + val)
        else:
            return None
        
        return new_date.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        return None

