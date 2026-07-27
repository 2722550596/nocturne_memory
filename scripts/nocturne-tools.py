#!/usr/bin/env python3
"""
tools/nocturne-tools.py — Nocturne Memory 快捷管理工具

用法：
  python tools/nocturne-tools.py ls                           # 列出所有命名空间的根节点
  python tools/nocturne-tools.py ls elias                     # 列出 elias 的记忆树
  python tools/nocturne-tools.py ls elias identity            # 列出 identity 子节点
  python tools/nocturne-tools.py get elias core://identity    # 读取指定节点
  python tools/nocturne-tools.py put elias observations/mingrui_first "第一次见到周明瑞..."
  python tools/nocturne-tools.py put elias observations/mingrui_first "新内容" --update
  python tools/nocturne-tools.py edit elias core://observations/mingrui_first --content "修正后的内容"
  python tools/nocturne-tools.py rm elias core://observations/mingrui_first
  python tools/nocturne-tools.py mv elias core://observations/old_name new_name
  python tools/nocturne-tools.py search elias "周明瑞"
  python tools/nocturne-tools.py search --all "关键词"        # 搜索所有命名空间
  python tools/nocturne-tools.py tree elias                   # 完整树形图
  python tools/nocturne-tools.py export elias > elias.md      # 导出为 markdown
  python tools/nocturne-tools.py import elias memories.md     # 从 markdown 批量导入
  python tools/nocturne-tools.py glossary elias               # 查看词典
  python tools/nocturne-tools.py glossary elias --export out.json  # 导出词库
  python tools/nocturne-tools.py glossary elias --import out.json  # 导入词库
  python tools/nocturne-tools.py glossary elias --scan             # 扫描命中情况
  python tools/nocturne-tools.py namespaces                   # 列出所有命名空间
  python tools/nocturne-tools.py diff elias core://identity   # 查看节点版本历史
  python tools/nocturne-tools.py rollback elias --list        # 列出 deprecated 记忆
  python tools/nocturne-tools.py rollback elias --id 599      # 按 ID 回滚 deprecated 记忆
  python tools/nocturne-tools.py rollback elias --uri core://identity/habits  # 按 URI 回滚
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

# ── 配置 ──────────────────────────────────────────────────────────────────────

API_BASE = "http://127.0.0.1:8233"



# ── 字符规范化 ────────────────────────────────────────────────────────────────

# 引号映射表：所有引号变体 → ASCII 引号
_QUOTE_MAP = {
    "\u201c": '"',  # 左双引号 “
    "\u201d": '"',  # 右双引号 ”
    "\u2018": "'",  # 左单引号 ‘
    "\u2019": "'",  # 右单引号 ’
    "\u300c": '"',  # 「 → "
    "\u300d": '"',  # 」 → "
    "\u300e": '"',  # 『 → "
    "\u300f": '"',  # 』 → "
    "\uff02": '"',  # 全角双引号 ＂
    "\uff07": "'",  # 全角单引号 ＇
    "\u2014": '-',  # em dash — → -
    "\u2013": '-',  # en dash – → -
    "\uff0d": '-',  # 全角减号 － → -
    "\u3000": ' ',  # 全角空格 → 半角
    "\u00a0": ' ',  # 不间断空格 → 半角
}

_QUOTE_TABLE = str.maketrans(_QUOTE_MAP)


def normalize_text(s: str) -> str:
    """规范化文本：统一引号、破折号、空格等字符变体。"""
    return s.translate(_QUOTE_TABLE)


# ── HTTP 工具 ──────────────────────────────────────────────────────────────────

def api_get(path: str) -> dict:
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"✗ {e.code}: {body}", file=sys.stderr)
        sys.exit(1)


def api_post(path: str, body: dict) -> dict:
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"✗ {e.code}: {body_text}", file=sys.stderr)
        sys.exit(1)


def api_put(path: str, body: dict) -> dict:
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="PUT")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"✗ {e.code}: {body_text}", file=sys.stderr)
        sys.exit(1)


def api_delete(path: str) -> dict:
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={"Content-Type": "application/json"}, method="DELETE")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"✗ {e.code}: {body_text}", file=sys.stderr)
        sys.exit(1)


# ── URI 解析 ──────────────────────────────────────────────────────────────────

def parse_uri(uri: str) -> tuple[str, str]:
    """解析 core://identity 为 (domain, path)"""
    if "://" in uri:
        domain, path = uri.split("://", 1)
        return domain, path
    return "core", uri


def ns_param(namespace: str, prefix: str = "&") -> str:
    """返回 namespace 查询参数。prefix='&' 用于追加到已有参数后面，prefix='?' 用于作为首个参数。"""
    return f"{prefix}namespace={urllib.parse.quote(namespace)}"


# ── 命令实现 ──────────────────────────────────────────────────────────────────

def cmd_namespaces(args):
    """列出所有命名空间"""
    result = api_get("/api/browse/namespaces")
    for ns in result:
        if ns:
            print(ns)
        else:
            print("(default)")


def cmd_ls(args):
    """列出节点"""
    ns = args.namespace
    path = args.path or ""
    domain = args.domain

    if not path:
        data = api_get(f"/api/browse/node?domain={domain}&path=&{ns_param(ns)}")
    else:
        data = api_get(f"/api/browse/node?domain={domain}&path={urllib.parse.quote(path, safe='/')}&{ns_param(ns)}")

    children = data.get("children", [])
    if not children:
        print("(空)")
        return

    for child in children:
        p = child["path"]
        disc = child.get("disclosure", "")
        snippet = child.get("content_snippet", "").split("\n")[0][:60]
        count = child.get("approx_children_count", 0)
        suffix = f" [{count}]" if count > 0 else ""
        priority = child.get("priority", "?")
        disc_str = f"  ← {disc}" if disc else ""
        print(f"  {'P'+str(priority).ljust(3)} {p}{suffix}{disc_str}")
        if snippet:
            print(f"       {snippet}")


def cmd_get(args):
    """读取节点详情"""
    domain, path = parse_uri(args.uri)
    data = api_get(f"/api/browse/node?domain={domain}&path={urllib.parse.quote(path, safe='/')}&{ns_param(args.namespace)}")

    node = data["node"]
    children = data.get("children", [])

    print(f"URI: {node['uri']}")
    print(f"Priority: {node['priority']}")
    if node.get("disclosure"):
        print(f"Disclosure: {node['disclosure']}")
    if node.get("created_at"):
        print(f"Created: {node['created_at']}")
    if node.get("aliases"):
        print(f"Aliases: {', '.join(node['aliases'])}")
    if node.get("glossary_keywords"):
        print(f"Glossary: {', '.join(node['glossary_keywords'])}")
    print(f"UUID: {node.get('node_uuid', '?')}")
    print("─" * 60)
    content = node.get("content", "")
    if content:
        print(content)
    else:
        print("(无内容)")

    if children:
        print("\n子节点:")
        for child in children:
            count = child.get("approx_children_count", 0)
            suffix = f" [{count}]" if count > 0 else ""
            print(f"  └─ {child['path']}{suffix}")


def cmd_put(args):
    """创建或更新记忆"""
    domain, path = parse_uri(args.uri)
    
    # Determine if we should update or create
    if args.update:
        try:
            node = api_get(f"/browse/node?domain={domain}&path={urllib.parse.quote(path)}&namespace={urllib.parse.quote(args.namespace)}")
            if node:
                # Perform update instead
                body = {
                    "content": normalize_text(args.content),
                    "priority": args.priority,
                    "disclosure": args.disclosure or node.get("disclosure", "Manual Entry"),
                    "world_timestamp": args.time or node.get("world_timestamp")
                }
                api_put(f"/browse/node?domain={domain}&path={urllib.parse.quote(path)}&namespace={urllib.parse.quote(args.namespace)}", body)
                print(f"✓ 已更新: {domain}://{path}")
                return
        except:
            pass

    body = {
        "parent_path": path.rsplit("/", 1)[0] if "/" in path else "",
        "title": path.rsplit("/", 1)[-1],
        "content": normalize_text(args.content),
        "disclosure": args.disclosure or "Manual Entry",
        "domain": domain,
        "priority": args.priority,
        "world_timestamp": args.time
    }
    
    res = api_post(f"/browse/node?namespace={urllib.parse.quote(args.namespace)}", body)
    uri = res["uri"]
    print(f"✓ 已创建: {uri}")
    if args.time:
        print(f"  时间点: {args.time}")


def cmd_edit(args):
    """编辑节点内容"""
    domain, path = parse_uri(args.uri)
    
    # Fetch current state first
    node = api_get(f"/browse/node?domain={domain}&path={urllib.parse.quote(path)}&namespace={urllib.parse.quote(args.namespace)}")
    
    content = args.content if args.content is not None else node["content"]
    priority = args.priority if args.priority is not None else node["priority"]
    disclosure = args.disclosure if args.disclosure is not None else node["disclosure"]
    world_timestamp = args.time if args.time is not None else node.get("world_timestamp")

    # Handle specialized edits (find-replace, append, etc.)
    if args.find_replace:
        old, new = args.find_replace
        content = content.replace(old, new)
    
    if args.append:
        content = content.rstrip() + "\n\n" + args.append

    if args.line is not None and args.content is not None:
        lines = content.splitlines()
        if 1 <= args.line <= len(lines):
            lines[args.line - 1] = args.content
            content = "\n".join(lines)

    if args.delete_line is not None:
        lines = content.splitlines()
        if 1 <= args.delete_line <= len(lines):
            lines.pop(args.delete_line - 1)
            content = "\n".join(lines)

    if args.insert_after is not None and args.content is not None:
        lines = content.splitlines()
        if 0 <= args.insert_after <= len(lines):
            lines.insert(args.insert_after, args.content)
            content = "\n".join(lines)

    if args.smart_match:
        content = normalize_text(content)

    body = {
        "content": content,
        "priority": priority,
        "disclosure": disclosure,
        "world_timestamp": world_timestamp
    }
    
    api_put(f"/browse/node?domain={domain}&path={urllib.parse.quote(path)}&namespace={urllib.parse.quote(args.namespace)}", body)
    print(f"✓ 已修改: {domain}://{path}")
    if args.time:
        print(f"  时间点更新为: {args.time}")

def cmd_rm(args):
    """删除节点"""
    domain, path = parse_uri(args.uri)
    ns = args.namespace

    if not args.force:
        # 先展示节点信息
        try:
            data = api_get(f"/api/browse/node?domain={domain}&path={urllib.parse.quote(path, safe='/')}&{ns_param(ns)}")
            content = data["node"].get("content", "")[:200]
            children = data.get("children", [])
            print(f"即将删除: {domain}://{path}")
            if content:
                print(f"内容: {content}")
            if children:
                print(f"子节点: {len(children)} 个 (会被一并删除)")
                for c in children:
                    print(f"  └─ {c['path']}")
        except SystemExit:
            pass

        confirm = input("\n确认删除? [y/N] ").strip().lower()
        if confirm not in ("y", "yes"):
            print("取消")
            return

    result = api_delete(
        f"/api/browse/node?domain={domain}&path={urllib.parse.quote(path, safe='/')}&{ns_param(ns)}"
    )
    print(f"✓ 已删除: {domain}://{path}")


def cmd_mv(args):
    """重命名节点"""
    domain, old_path = parse_uri(args.uri)
    ns = args.namespace
    new_name = args.new_name

    body = {
        "path": old_path,
        "new_name": new_name,
        "domain": domain,
    }
    result = api_post(f"/api/browse/node/rename?{ns_param(ns)}", body)
    print(f"✓ {result['old_uri']} → {result['new_uri']}")


def cmd_search(args):
    """搜索记忆"""
    query = args.query
    ns = args.namespace
    domain_filter = args.domain  # "" means all domains

    if args.all:
        # 搜索所有命名空间
        namespaces = api_get("/api/browse/namespaces")
        total = 0
        for ns_name in namespaces:
            ns_display = ns_name or "(default)"
            try:
                result = api_get(f"/api/browse/search?q={urllib.parse.quote(query)}&namespace={urllib.parse.quote(ns_name)}")
                results = result.get("results", [])
                if domain_filter:
                    results = [r for r in results if r.get("uri", "").startswith(f"{domain_filter}://")]
                if results:
                    print(f"\n[{ns_display}] 找到 {len(results)} 条:")
                    for r in results:
                        snippet = r.get("snippet", r.get("content_snippet", ""))[:80]
                        print(f"  {r['uri']}: {snippet}")
                    total += len(results)
            except Exception:
                pass
        if total == 0:
            print("未找到相关记忆。")
        else:
            print(f"\n共 {total} 条结果")
    else:
        result = api_get(f"/api/browse/search?q={urllib.parse.quote(query)}&namespace={urllib.parse.quote(ns)}")
    results = result if isinstance(result, list) else result.get("results", [])
    if domain_filter:
        results = [r for r in results if r.get("uri", "").startswith(f"{domain_filter}://")]
        if not results:
            print("未找到相关记忆。")
            return
        print(f"找到 {len(results)} 条:\n")
        for r in results:
            snippet = r.get("snippet", r.get("content_snippet", ""))[:100]
            print(f"  {r['uri']}: {snippet}")


def cmd_tree(args):
    """显示完整树形图"""
    ns = args.namespace
    max_depth = args.depth
    domain = args.domain
    all_domains = getattr(args, 'all_domains', False)

    def render(path, depth, prefix="", cur_domain="core"):
        if depth > max_depth:
            return
        try:
            url = f"/api/browse/node?domain={cur_domain}&path={urllib.parse.quote(path, safe='/')}&nav_only=true&{ns_param(ns)}"
            data = api_get(url)
        except SystemExit:
            return

        children = data.get("children", [])
        for i, child in enumerate(children):
            is_last = i == len(children) - 1
            connector = "└── " if is_last else "├── "
            child_prefix = "    " if is_last else "│   "

            p = child["path"]
            disc = child.get("disclosure", "")
            count = child.get("approx_children_count", 0)
            snippet = child.get("content_snippet", "").split("\n")[0][:40]

            disc_str = f"  ← {disc}" if disc else ""
            count_str = f" [{count}]" if count > 0 else ""

            print(f"{prefix}{connector}{p}{count_str}{disc_str}")
            if snippet and depth < max_depth:
                print(f"{prefix}{child_prefix}  {snippet}")

            render(p, depth + 1, prefix + child_prefix, cur_domain)

    print(f"记忆树 [{ns}]")

    if all_domains:
        # 尝试已知域名
        for d in ["core", "history"]:
            try:
                test_url = f"/api/browse/node?domain={d}&path=&nav_only=true&{ns_param(ns)}"
                api_get(test_url)
                print(f"{d}://")
                render("", 1, "", d)
            except SystemExit:
                pass
    else:
        print(f"{domain}://")
        render("", 1, "", domain)


def cmd_export(args):
    """导出为 markdown"""
    ns = args.namespace
    domain_filter = args.domain  # "" means all domains

    # 确定要导出的域名列表
    domains = []
    if domain_filter:
        domains = [domain_filter]
    else:
        # 尝试已知域名
        for d in ["core", "history"]:
            try:
                test_url = f"/api/browse/node?domain={d}&path=&{ns_param(ns)}"
                api_get(test_url)
                domains.append(d)
            except SystemExit:
                pass

    def export_node(path, depth=0, cur_domain="core"):
        lines = []
        try:
            url = f"/api/browse/node?domain={cur_domain}&path={urllib.parse.quote(path, safe='/')}&{ns_param(ns)}"
            data = api_get(url)
        except SystemExit:
            return lines

        node = data["node"]
        children = data.get("children", [])
        indent = "#" * min(depth + 1, 6)

        if node.get("content") and not node.get("is_virtual"):
            uri = node["uri"]
            lines.append(f"{indent} {uri}")
            if node.get("disclosure"):
                lines.append(f"<!-- disclosure: {node['disclosure']} -->")
            lines.append(f"<!-- priority: {node['priority']} -->")
            lines.append(node["content"])
            lines.append("")

        for child in children:
            child_path = child["path"]
            lines.extend(export_node(child_path, depth + 1, cur_domain))

        return lines

    all_lines = [f"# 记忆导出: {ns}\n"]
    for d in domains:
        all_lines.extend(export_node("", 0, d))
    print("\n".join(all_lines))


def cmd_import(args):
    """从 markdown 批量导入"""
    ns = args.namespace
    filepath = Path(args.file)
    if not filepath.exists():
        print(f"✗ 文件不存在: {filepath}", file=sys.stderr)
        sys.exit(1)

    content = filepath.read_text(encoding="utf-8")
    created = 0
    errors = 0

    # 解析 markdown 格式
    # 格式：
    # # core://identity
    # <!-- disclosure: 触发条件 -->
    # <!-- priority: 0 -->
    # 内容
    #
    # ## core://identity/detail
    # ...

    sections = re.split(r'^(#{1,6}) (.+://.+)$', content, flags=re.MULTILINE)

    i = 1  # sections[0] is before first match
    while i < len(sections):
        level = sections[i]  # #
        uri_line = sections[i + 1] if i + 1 < len(sections) else ""
        body = sections[i + 2] if i + 2 < len(sections) else ""
        i += 3

        if not uri_line:
            continue

        domain, path = parse_uri(uri_line.strip())
        parts = path.split("/")
        title = parts[-1]
        parent = "/".join(parts[:-1]) if len(parts) > 1 else ""

        # 提取 disclosure 和 priority
        disclosure = None
        priority = 5
        disc_match = re.search(r'<!--\s*disclosure:\s*(.+?)\s*-->', body)
        if disc_match:
            disclosure = disc_match.group(1)
        pri_match = re.search(r'<!--\s*priority:\s*(\d+)\s*-->', body)
        if pri_match:
            priority = int(pri_match.group(1))

        # 清理注释行
        body_clean = re.sub(r'<!--.*?-->', '', body).strip()

        if not body_clean:
            continue

        post_body = {
            "parent_path": parent,
            "title": title,
            "content": body_clean,
            "priority": priority,
            "domain": domain,
        }
        if disclosure is not None:
            post_body["disclosure"] = disclosure

        try:
            result = api_post(f"/api/browse/node?{ns_param(ns)}", post_body)
            created += 1
            print(f"  ✓ {domain}://{path}")
        except SystemExit:
            # 可能已存在，尝试更新
            try:
                update_body = {"content": body_clean, "priority": priority}
                if disclosure is not None:
                    update_body["disclosure"] = disclosure
                api_put(
                    f"/api/browse/node?domain={domain}&path={urllib.parse.quote(path, safe='/')}&{ns_param(ns)}",
                    update_body
                )
                print(f"  ↻ {domain}://{path} (已更新)")
                created += 1
            except SystemExit:
                print(f"  ✗ {domain}://{path} (失败)")
                errors += 1

    print(f"\n完成: {created} 条已导入, {errors} 条失败")


def cmd_glossary(args):
    """查看/管理词典"""
    ns = args.namespace

    if args.add_keyword and args.glossary_uri:
        # 添加词典绑定
        domain, path = parse_uri(args.glossary_uri)
        data = api_get(f"/api/browse/node?domain={domain}&path={urllib.parse.quote(path, safe='/')}&{ns_param(ns)}")
        node_uuid = data["node"]["node_uuid"]
        result = api_post(f"/api/browse/glossary?{ns_param(ns)}", {
            "keyword": args.add_keyword,
            "node_uuid": node_uuid,
        })
        print(f"✓ 已绑定: \"{args.add_keyword}\" → {args.glossary_uri}")

    elif args.rm_keyword and args.glossary_uri:
        # 删除词典绑定
        domain, path = parse_uri(args.glossary_uri)
        data = api_get(f"/api/browse/node?domain={domain}&path={urllib.parse.quote(path, safe='/')}&{ns_param(ns)}")
        node_uuid = data["node"]["node_uuid"]
        result = api_delete(f"/api/browse/glossary?{ns_param(ns)}")
        # DELETE 需要 body，用另一种方式
        url = f"{API_BASE}/api/browse/glossary?{ns_param(ns)}"
        data_bytes = json.dumps({"keyword": args.rm_keyword, "node_uuid": node_uuid}).encode("utf-8")
        req = urllib.request.Request(url, data=data_bytes, headers={"Content-Type": "application/json"}, method="DELETE")
        try:
            with urllib.request.urlopen(req) as resp:
                pass
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            print(f"✗ {e.code}: {body_text}", file=sys.stderr)
            sys.exit(1)
        print(f"✓ 已解绑: \"{args.rm_keyword}\" → {args.glossary_uri}")

    elif args.export_glossary:
        # 导出词库到 JSON 文件
        result = api_get(f"/api/browse/glossary?{ns_param(ns)}")
        entries = result.get("glossary", [])
        if not entries:
            print("(词典为空，无导出内容)")
            return
        export_data = []
        for entry in entries:
            keyword = entry.get("keyword", "")
            for node in entry.get("nodes", []):
                uri = node.get("uri", "")
                if uri and not uri.startswith("unlinked://"):
                    export_data.append({"keyword": keyword, "uri": uri})
        out_path = args.export_glossary
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)
        print(f"✓ 已导出 {len(export_data)} 条绑定到 {out_path}")

    elif args.import_glossary:
        # 从 JSON 文件批量导入词库
        in_path = args.import_glossary
        if not os.path.isfile(in_path):
            print(f"✗ 文件不存在: {in_path}", file=sys.stderr)
            sys.exit(1)
        with open(in_path, "r", encoding="utf-8") as f:
            import_data = json.load(f)
        if not isinstance(import_data, list):
            print("✗ JSON 文件格式错误：应为数组 [{\"keyword\": ..., \"uri\": ...}]", file=sys.stderr)
            sys.exit(1)
        ok = 0
        fail = 0
        seen = set()
        for item in import_data:
            kw = item.get("keyword", "").strip()
            uri = item.get("uri", "").strip()
            if not kw or not uri:
                print(f"  ✗ 跳过: keyword 或 uri 为空", file=sys.stderr)
                fail += 1
                continue
            dedup_key = (kw, uri)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            try:
                domain, path = parse_uri(uri)
                data = api_get(f"/api/browse/node?domain={domain}&path={urllib.parse.quote(path, safe='/')}&{ns_param(ns)}")
                node_uuid = data["node"]["node_uuid"]
                api_post(f"/api/browse/glossary?{ns_param(ns)}", {
                    "keyword": kw,
                    "node_uuid": node_uuid,
                })
                print(f"  ✓ \"{kw}\" → {uri}")
                ok += 1
            except Exception as e:
                print(f"  ✗ \"{kw}\" → {uri}: {e}", file=sys.stderr)
                fail += 1
        print(f"\n导入完成: {ok} 成功, {fail} 失败")

    elif args.scan_glossary:
        # 扫描所有记忆，显示每个关键词被检出的位置
        result = api_get(f"/api/browse/glossary?{ns_param(ns)}")
        entries = result.get("glossary", [])
        if not entries:
            print("(词典为空)")
            return
        keywords = [e.get("keyword", "") for e in entries if e.get("keyword")]
        if not keywords:
            print("(词典为空)")
            return
        print(f"扫描 {len(keywords)} 个关键词...\n")
        domain_for_scan = getattr(args, 'domain', '')
        scan_domains = [domain_for_scan] if domain_for_scan else ["core", "history"]

        # 遍历指定域的所有叶子节点，检查内容
        def scan_node(path, domain="core"):
            try:
                encoded = urllib.parse.quote(path, safe='/') if path else ""
                data = api_get(f"/api/browse/node?domain={domain}&path={encoded}&{ns_param(ns)}")
                results = []
                node = data.get("node", {})
                content = node.get("content", "")
                uri = node.get("uri", "")
                node_uuid = node.get("node_uuid", "")
                if content:
                    for kw in keywords:
                        if kw in content:
                            # 排除自身节点
                            target_entries = [e for e in entries if e.get("keyword") == kw]
                            is_self = False
                            for te in target_entries:
                                for tn in te.get("nodes", []):
                                    if tn.get("node_uuid") == node_uuid:
                                        is_self = True
                                        break
                            results.append((kw, uri, is_self))
                for child in data.get("children", []):
                    child_path = child.get("path", "")
                    if child_path:
                        results.extend(scan_node(child_path, domain))
                return results
            except Exception:
                return []

        matches = []
        for d in scan_domains:
            matches.extend(scan_node("", d))
        # 按关键词分组
        from collections import defaultdict
        grouped = defaultdict(list)
        for kw, uri, is_self in matches:
            grouped[kw].append((uri, is_self))
        for kw in sorted(grouped.keys()):
            locations = grouped[kw]
            print(f"  \"{kw}\" 出现在 {len(locations)} 个节点:")
            for uri, is_self in locations:
                tag = " (自身)" if is_self else ""
                print(f"    - {uri}{tag}")
            print()

    else:
        # 查看词典
        result = api_get(f"/api/browse/glossary?{ns_param(ns)}")
        entries = result.get("glossary", [])
        if not entries:
            print("(词典为空)")
            return
        print(f"词典 [{ns}] ({len(entries)} 条):\n")
        for entry in entries:
            keyword = entry.get("keyword", "")
            nodes = entry.get("nodes", [])
            node_str = ", ".join(
                n.get("uri", n.get("path", "?")) for n in nodes
            )
            print(f"  \"{keyword}\" → {node_str}")

def cmd_batch(args):
    """批量操作：搜索匹配的节点后批量修改"""
    ns = args.namespace
    query = args.search
    domain = args.domain

    # 1. 搜索匹配节点
    result = api_get(f"/api/browse/search?q={urllib.parse.quote(query)}&namespace={urllib.parse.quote(ns)}")
    results = result.get("results", [])
    if not results:
        print("未找到匹配的记忆。")
        return

    # 2. 展示将要操作的节点（按域名过滤）
    if domain:
        results = [r for r in results if r.get("uri", "").startswith(f"{domain}://")]
    print(f"找到 {len(results)} 条匹配记忆:\n")
    for i, r in enumerate(results):
        snippet = r.get("snippet", r.get("content_snippet", ""))[:60]
        print(f"  [{i+1}] {r['uri']}: {snippet}")

    op_desc = []
    if args.find_replace:
        op_desc.append(f"查找替换: \"{args.find_replace[0][:30]}\" → \"{args.find_replace[1][:30]}\"")
    if args.set_priority is not None:
        op_desc.append(f"设置优先级: {args.set_priority}")
    if args.set_disclosure:
        op_desc.append(f"设置触发条件: {args.set_disclosure}")

    if not op_desc:
        print("\n✗ 未指定操作（--find-replace / --set-priority / --set-disclosure）", file=sys.stderr)
        sys.exit(1)

    print(f"\n将执行: {'; '.join(op_desc)}")

    # 3. 确认
    if not args.confirm:
        confirm = input("\n确认执行? [y/N] ").strip().lower()
        if confirm not in ("y", "yes"):
            print("取消")
            return

    # 4. 逐个执行
    success = 0
    failed = 0
    skipped = 0
    for r in results:
        uri = r["uri"]
        r_domain, r_path = parse_uri(uri)
        try:
            node_data = api_get(
                f"/api/browse/node?domain={r_domain}&path={urllib.parse.quote(r_path, safe='/')}&{ns_param(ns)}"
            )
            current_content = node_data["node"]["content"]
        except SystemExit:
            print(f"  ✗ {uri}: 读取失败")
            failed += 1
            continue

        body = {}
        new_content = current_content
        changed = False

        # 查找替换
        if args.find_replace:
            old, new = args.find_replace
            if getattr(args, 'smart_match', False):
                norm_old = normalize_text(old)
                norm_content = normalize_text(new_content)
                if norm_old in norm_content:
                    # 简化处理：规范化后替换
                    new_content = new_content.replace(old, new)
                    # 如果精确替换没命中（引号不同），用规范化方式
                    if old not in current_content:
                        # 用规范化方式重建
                        for char_old, char_new in _QUOTE_MAP.items():
                            old_variant = old.replace(char_new, char_old)
                            if old_variant in new_content:
                                new_content = new_content.replace(old_variant, new)
                                break
                    changed = True
                else:
                    skipped += 1
            else:
                if old in new_content:
                    new_content = new_content.replace(old, new)
                    changed = True
                else:
                    skipped += 1

        if changed or args.set_priority is not None or args.set_disclosure:
            if changed:
                body["content"] = new_content
            if args.set_priority is not None:
                body["priority"] = args.set_priority
            if args.set_disclosure is not None:
                body["disclosure"] = args.set_disclosure
            try:
                api_put(
                    f"/api/browse/node?domain={r_domain}&path={urllib.parse.quote(r_path, safe='/')}&{ns_param(ns)}",
                    body
                )
                print(f"  ✓ {uri}")
                success += 1
            except SystemExit:
                print(f"  ✗ {uri}: 写入失败")
                failed += 1
        else:
            skipped += 1

    print(f"\n完成: {success} 成功, {failed} 失败, {skipped} 跳过")


def cmd_rollback(args):
    """回滚 deprecated 记忆"""
    ns = args.namespace

    if args.list:
        # 列出 deprecated 记忆
        result = api_get(f"/api/review/deprecated?{ns_param(ns, prefix='?')}")
        memories = result.get("memories", [])
        if not memories:
            print("没有 deprecated 记忆。")
            return
        keyword = getattr(args, 'search', None)
        if keyword:
            memories = [m for m in memories if keyword.lower() in m.get("content_snippet", "").lower()]
            if not memories:
                print(f"未找到包含 '{keyword}' 的 deprecated 记忆。")
                return
        print(f"deprecated 记忆 [{ns or '(default)'}] ({len(memories)} 条):\n")
        for m in memories:
            mid = m["id"]
            snippet = m.get("content_snippet", "").split("\n")[0][:70]
            migrated = m.get("migrated_to", "?")
            created = m.get("created_at", "?")[:19]
            print(f"  id={mid}  migrated_to={migrated}  {created}")
            print(f"    {snippet}")
            print()
        return

    if args.uri:
        # 按 URI 查找对应 node 的 deprecated 记忆
        domain, path = parse_uri(args.uri)
        # 1. 先解析 URI 获取 node_uuid
        try:
            node_data = api_get(
                f"/api/browse/node?domain={domain}&path={urllib.parse.quote(path, safe='/')}&{ns_param(ns)}"
            )
            node_uuid = node_data["node"]["node_uuid"]
        except (SystemExit, KeyError):
            print(f"✗ 无法解析 URI: {args.uri}", file=sys.stderr)
            return
        # 2. 搜索 deprecated 记忆，按 node_uuid 匹配
        result = api_get(f"/api/review/deprecated?{ns_param(ns, prefix='?')}")
        memories = result.get("memories", [])
        candidates = [m for m in memories if m.get("node_uuid") == node_uuid]
        if not candidates:
            print(f"未找到 {args.uri} 对应的 deprecated 记忆。")
            return
        # 取最新的（id 最大的）
        target = max(candidates, key=lambda m: m["id"])
        args.memory_id = target["id"]
        snippet = target.get("content_snippet", "")[:60].replace("\n", " ")
        print(f"找到 deprecated 记忆 id={target['id']}: {snippet}")

    if args.memory_id:
        mid = args.memory_id
        try:
            result = api_post(f"/api/review/deprecated/{mid}/rollback?{ns_param(ns, prefix='?')}", {})
            if result.get("success"):
                restored_id = result.get("restored_memory_id", mid)
                node_uuid = result.get("node_uuid", "?")
                print(f"✓ 已回滚: memory_id={restored_id}, node_uuid={node_uuid[:8]}...")
            else:
                print(f"✗ 回滚失败: {result.get('message', '未知错误')}", file=sys.stderr)
        except SystemExit:
            pass  # api_post 已打印错误
        return

    # 什么都没指定
    print("用法: rollback <namespace> --list [--search <关键词>]  # 列出 deprecated 记忆")
    print("      rollback <namespace> --id <memory_id>              # 按 ID 回滚")
    print("      rollback <namespace> --uri <uri>                   # 按 URI 回滚")

def cmd_diff(args):
    """查看节点版本历史（通过 review API）"""
    domain, path = parse_uri(args.uri)
    ns = args.namespace

    # 尝试获取 review 信息
    try:
        result = api_get(f"/api/review/changesets?{ns_param(ns)}")
        changesets = result if isinstance(result, list) else result.get("changesets", result.get("items", []))
        relevant = []
        for cs in changesets:
            cs_path = cs.get("target_path", "")
            if cs_path == path or cs_path.startswith(path + "/"):
                relevant.append(cs)

        if not relevant:
            print(f"无版本历史: {domain}://{path}")
            return

        print(f"版本历史: {domain}://{path}\n")
        for cs in relevant:
            status = cs.get("status", "?")
            action = cs.get("action", "?")
            ts = cs.get("created_at", "?")
            old = cs.get("old_content", "")[:100]
            new = cs.get("new_content", "")[:100]
            print(f"  [{status}] {action} @ {ts}")
            if old:
                print(f"    - {old}")
            if new:
                print(f"    + {new}")
    except SystemExit:
        print("无法获取版本历史（review API 不可用）")


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Nocturne Memory 快捷管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s ls elias                          # 列出 elias 的记忆
  %(prog)s ls elias observations             # 列出 observations 子节点
  %(prog)s get elias core://identity         # 读取身份记忆
  %(prog)s put elias observations/mingrui_0603 "他今天穿了灰色卫衣" --priority 3
  %(prog)s edit elias core://identity --content "新的身份描述"
  %(prog)s rm elias core://observations/old  # 删除节点
  %(prog)s mv elias core://observations/old new_name
  %(prog)s search elias "周明瑞"              # 搜索
  %(prog)s tree elias --depth 3              # 树形图
  %(prog)s export elias > backup.md          # 导出
  %(prog)s import elias backup.md            # 导入
  %(prog)s glossary elias                    # 查看词典
  %(prog)s glossary elias --export out.json  # 导出词库到 JSON
  %(prog)s glossary elias --import out.json  # 从 JSON 导入词库
  %(prog)s glossary elias --scan             # 扫描关键词命中情况
  %(prog)s rollback elias --list             # 列出 deprecated 记忆
  %(prog)s rollback elias --id 599           # 按 ID 回滚
  %(prog)s rollback elias --uri core://identity/habits  # 按 URI 回滚
        """,
    )
    parser.add_argument("--api", default="http://127.0.0.1:8233", help="Nocturne API 地址")

    sub = parser.add_subparsers(dest="command", help="命令")

    # namespaces
    sub.add_parser("namespaces", help="列出所有命名空间")

    # ls
    p_ls = sub.add_parser("ls", aliases=["list"], help="列出节点")
    p_ls.add_argument("namespace", nargs="?", default="", help="命名空间")
    p_ls.add_argument("path", nargs="?", default="", help="路径")
    p_ls.add_argument("--domain", default="core", help="域名 (默认 core)")
    # get
    p_get = sub.add_parser("get", help="读取节点详情")
    p_get.add_argument("namespace", help="命名空间")
    p_get.add_argument("uri", help="URI，如 core://identity")

    # put
    p_put = sub.add_parser("put", help="创建记忆")
    p_put.add_argument("namespace", help="命名空间")
    p_put.add_argument("uri", help="URI (e.g. core://identity)")
    p_put.add_argument("content", help="记忆内容")
    p_put.add_argument("--priority", "-p", type=int, default=5, help="优先级 0-10 (默认 5)")
    p_put.add_argument("--disclosure", "-d", default="", help="触发条件")
    p_put.add_argument("--time", help="世界观时间 (YYYY-MM-DD)")
    p_put.add_argument("--update", "-u", action="store_true", help="如已存在则更新")
    p_put.set_defaults(func=cmd_put)

    # edit
    p_edit = sub.add_parser("edit", help="编辑节点")
    p_edit.add_argument("namespace", help="命名空间")
    p_edit.add_argument("uri", help="URI")
    p_edit.add_argument("--content", "-c", help="新内容")
    p_edit.add_argument("--priority", "-p", type=int, help="新优先级")
    p_edit.add_argument("--disclosure", "-d", help="新触发条件")
    p_edit.add_argument("--time", help="新世界观时间 (可选)")
    p_edit.add_argument("--find-replace", "-f", nargs=2, metavar=("OLD", "NEW"), help="查找替换")
    p_edit.add_argument("--append", "-a", help="追加内容到末尾")
    p_edit.add_argument("--line", "-l", type=int, help="按行号替换")
    p_edit.add_argument("--delete-line", type=int, help="按行号删除")
    p_edit.add_argument("--insert-after", type=int, help="在指定行后插入")
    p_edit.add_argument("--smart-match", "-S", action="store_true", help="智能匹配")
    p_edit.set_defaults(func=cmd_edit)

    # batch
    p_batch = sub.add_parser("batch", help="批量操作")
    p_batch.add_argument("namespace", help="命名空间")
    p_batch.add_argument("--search", "-s", required=True, help="搜索关键词（选择要操作的节点）")
    p_batch.add_argument("--find-replace", "-f", nargs=2, metavar=("OLD", "NEW"), help="查找替换")
    p_batch.add_argument("--set-priority", type=int, help="批量设置优先级")
    p_batch.add_argument("--set-disclosure", help="批量设置触发条件")
    p_batch.add_argument("--confirm", action="store_true", help="跳过确认直接执行")
    p_batch.add_argument("--domain", default="core", help="搜索的域名 (默认 core)")
    p_batch.add_argument("--smart-match", "-S", action="store_true", help="智能匹配：自动规范化引号、破折号等字符变体")

    # rm
    p_rm = sub.add_parser("rm", help="删除节点")
    p_rm.add_argument("namespace", help="命名空间")
    p_rm.add_argument("uri", help="URI")
    p_rm.add_argument("--force", "-f", action="store_true", help="跳过确认")

    # mv
    p_mv = sub.add_parser("mv", help="重命名节点")
    p_mv.add_argument("namespace", help="命名空间")
    p_mv.add_argument("uri", help="URI")
    p_mv.add_argument("new_name", help="新名称")

    # search
    p_search = sub.add_parser("search", aliases=["s"], help="搜索记忆")
    p_search.add_argument("namespace", nargs="?", default="", help="命名空间")
    p_search.add_argument("query", help="搜索关键词")
    p_search.add_argument("--all", "-a", action="store_true", help="搜索所有命名空间")
    p_search.add_argument("--domain", default="", help="域名过滤 (默认搜索所有域名)")

    # tree
    p_tree = sub.add_parser("tree", help="完整树形图")
    p_tree.add_argument("namespace", nargs="?", default="", help="命名空间")
    p_tree.add_argument("--depth", "-d", type=int, default=2, help="最大深度 (默认 2)")
    p_tree.add_argument("--domain", default="core", help="域名 (默认 core)")
    p_tree.add_argument("--all-domains", action="store_true", help="显示所有域名的树")


    # glossary
    p_gloss = sub.add_parser("glossary", help="查看/管理词典")
    p_gloss.add_argument("namespace", nargs="?", default="", help="命名空间")
    p_gloss.add_argument("--add-keyword", help="添加词典关键词")
    p_gloss.add_argument("--rm-keyword", help="删除词典关键词")
    p_gloss.add_argument("--uri", dest="glossary_uri", help="关联的节点 URI")
    p_gloss.add_argument("--export", dest="export_glossary", help="导出词库到 JSON 文件")
    p_gloss.add_argument("--import", dest="import_glossary", help="从 JSON 文件导入词库")
    p_gloss.add_argument("--scan", dest="scan_glossary", action="store_true", help="扫描记忆内容，显示关键词命中情况")
    p_gloss.add_argument("--domain", default="", help="扫描的域名 (默认扫描所有域名)")

    # rollback
    p_rb = sub.add_parser("rollback", aliases=["rb"], help="回滚 deprecated 记忆")
    p_rb.add_argument("namespace", help="命名空间")
    p_rb.add_argument("--list", "-l", action="store_true", help="列出 deprecated 记忆")
    p_rb.add_argument("--search", "-s", help="按关键词过滤（配合 --list 使用）")
    p_rb.add_argument("--id", type=int, dest="memory_id", help="按 memory ID 回滚")
    p_rb.add_argument("--uri", help="按 URI 查找并回滚（取最新 deprecated 版本）")

    # diff
    p_diff = sub.add_parser("diff", help="查看版本历史")
    p_diff.add_argument("namespace", help="命名空间")
    p_diff.add_argument("uri", help="URI")

    args = parser.parse_args()

    # 覆盖全局 API 地址
    global API_BASE
    API_BASE = args.api

    if not args.command:
        parser.print_help()
        sys.exit(0)

    cmd_map = {
        "namespaces": cmd_namespaces,
        "ls": cmd_ls, "list": cmd_ls,
        "get": cmd_get,
        "put": cmd_put,
        "edit": cmd_edit,
        "rm": cmd_rm,
        "mv": cmd_mv,
        "search": cmd_search, "s": cmd_search,
        "tree": cmd_tree,
        "export": cmd_export,
        "import": cmd_import,
        "glossary": cmd_glossary,
        "batch": cmd_batch,
        "rollback": cmd_rollback, "rb": cmd_rollback,
        "diff": cmd_diff,
    }

    cmd_func = cmd_map.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
