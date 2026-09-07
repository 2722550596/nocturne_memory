#!/usr/bin/env python3
"""
ns_migrate.py — nocturne-memory 跨 namespace 迁移工具

针对 SQLite 版 nocturne-memory 数据库的 namespace 级运维：
  stats         查看 namespace × domain 分布
  rename        原子重命名/交换 namespace（含 boot_uris keys 重映射）
  copy-domains  深拷贝指定 namespace 的若干 domain 到另一 namespace
  verify        全库一致性校验（uri 规范、派生表对齐、FK 完整）

背景（2026-09 elias 迁移）：跨 namespace 迁移容易出现的 uri 路径混乱，
根源是 search_documents.uri / fts / embeddings / boot_uris 各自持有
namespace 或路径信息，逐表手工 UPDATE 极易漏改。本工具把全部联动收口。

表结构约定：
  - namespaced 表：paths / search_documents / search_documents_fts /
    glossary_keywords / memory_access_logs / revisions /
    search_document_embeddings + presets.boot_uris (JSON keys)
  - 全局表：nodes / edges / memories（多 namespace 共享节点）
    故 copy-domains 做节点级深拷贝（新 UUID），避免源快照被污染。

用法示例：
  # elias → elias-before, elias-distilled → elias（一次性交换）
  python ns_migrate.py rename db.sqlite --swap elias:elias-before elias-distilled:elias

  # 把 elias-before 的非 core 域拷给新 elias
  python ns_migrate.py copy-domains db.sqlite elias-before elias \
      --domains diary,history,history_raw,maintenance

  # 校验
  python ns_migrate.py verify db.sqlite
"""

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime

ROOT_UUID = "00000000-0000-0000-0000-000000000000"

# 所有持有 namespace 字段的表 → namespace 列名
NS_TABLES = {
    "paths": "namespace",
    "search_documents": "namespace",
    "search_documents_fts": "namespace",
    "glossary_keywords": "namespace",
    "memory_access_logs": "namespace",
    "revisions": "namespace",
    "search_document_embeddings": "namespace",
}

FTS_COLUMNS = "namespace, domain, path, node_uuid, uri, content, disclosure, search_terms"


# ── 基础工具 ──────────────────────────────────────────────────────────────────

def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def backup(db_path: str) -> str:
    """SQLite 在线备份（WAL 安全，服务运行中可用）。返回备份路径。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = f"{db_path}.{stamp}.bak"
    src = sqlite3.connect(db_path, timeout=30)
    dst = sqlite3.connect(bak)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    return bak


def ns_row_count(conn: sqlite3.Connection, table: str, ns: str) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE namespace = ?", (ns,)
    ).fetchone()[0]


def ns_exists(conn: sqlite3.Connection, ns: str) -> bool:
    for table in NS_TABLES:
        if ns_row_count(conn, table, ns) > 0:
            return True
    for row in conn.execute("SELECT boot_uris FROM presets"):
        if ns in json.loads(row[0]):
            return True
    return False


def fail(msg: str):
    print(f"✗ {msg}")
    sys.exit(1)


def ok(msg: str):
    print(f"✓ {msg}")


# ── stats ────────────────────────────────────────────────────────────────────

def cmd_stats(args):
    conn = connect(args.db)
    print(f"{'namespace':<22}{'domain':<14}{'paths':>7}{'docs':>7}{'fts':>7}{'glossary':>9}{'embeds':>7}")
    print("-" * 73)
    rows = conn.execute(
        "SELECT namespace, domain, COUNT(*) AS n FROM paths "
        "GROUP BY namespace, domain ORDER BY namespace, domain"
    ).fetchall()
    for r in rows:
        ns, dom = r["namespace"] or "(default)", r["domain"]
        q = (r["namespace"], r["domain"])
        docs = conn.execute(
            "SELECT COUNT(*) FROM search_documents WHERE namespace=? AND domain=?", q
        ).fetchone()[0]
        fts = conn.execute(
            "SELECT COUNT(*) FROM search_documents_fts WHERE namespace=? AND domain=?", q
        ).fetchone()[0]
        glo = conn.execute(
            "SELECT COUNT(*) FROM glossary_keywords WHERE namespace=?", (r["namespace"],)
        ).fetchone()[0] if dom == "core" else ""
        emb = conn.execute(
            "SELECT COUNT(*) FROM search_document_embeddings WHERE namespace=? AND domain=?", q
        ).fetchone()[0]
        print(f"{ns:<22}{dom:<14}{r['n']:>7}{docs:>7}{fts:>7}{str(glo):>9}{emb:>7}")

    print("\npresets (boot_uris keys):")
    for p in conn.execute("SELECT id, name, is_active, boot_uris FROM presets ORDER BY id"):
        keys = ", ".join(repr(k) for k in json.loads(p["boot_uris"]))
        print(f"  #{p['id']} {p['name']:<24} active={p['is_active']}  keys=[{keys}]")
    conn.close()


# ── rename ────────────────────────────────────────────────────────────────────

def remap_boot_uris(conn: sqlite3.Connection, mapping: dict):
    """重写 presets.boot_uris 的 JSON keys（mapping: old → 最终新名）。

    presets 表不带 namespace 列，其 keys 在 rename 全程保持旧名，
    因此这里一次性从旧名映射到最终名即可。
    """
    for row in conn.execute("SELECT id, boot_uris FROM presets").fetchall():
        data = json.loads(row["boot_uris"])
        new = {mapping.get(k, k): v for k, v in data.items()}
        conn.execute(
            "UPDATE presets SET boot_uris=? WHERE id=?",
            (json.dumps(new, ensure_ascii=False), row["id"]),
        )


def cmd_rename(args):
    # 解析 old:new 对（--swap 多对 或 --old/--new 单对，二选一）
    if args.swap and (args.old or args.new):
        fail("--swap 与 --old/--new 互斥")
    pairs = []
    if args.swap:
        for spec in args.swap:
            if ":" not in spec:
                fail(f"--swap 格式应为 old:new，收到 '{spec}'")
            old, new = spec.split(":", 1)
            if old == new:
                fail(f"--swap '{spec}': 新旧名相同")
            pairs.append((old, new))
    elif args.old and args.new:
        if args.old == args.new:
            fail("新旧名相同")
        pairs.append((args.old, args.new))
    else:
        fail("需要 --swap old:new [old:new ...] 或 --old/--new")

    olds = [o for o, _ in pairs]
    news = [n for _, n in pairs]
    if len(set(olds)) != len(olds):
        fail(f"旧名重复: {olds}")
    if len(set(news)) != len(news):
        fail(f"新名重复: {news}")

    conn = connect(args.db)
    for old in olds:
        if not ns_exists(conn, old):
            fail(f"namespace '{old}' 不存在（所有 namespace 表与 boot_uris 中均无记录）")

    # 目标名占用检查：目标名已且有数据、且它本身不在本次改名链内 → 冲突
    for new in news:
        if new not in olds and ns_exists(conn, new):
            fail(f"目标名 '{new}' 已存在数据（且不在本次交换链内）")

    print("重命名计划:")
    for old, new in pairs:
        counts = {t: ns_row_count(conn, t, old) for t in NS_TABLES}
        total = sum(counts.values())
        print(f"  {old!r} → {new!r}  ({total} 行 namespace 数据)")
        for t, n in counts.items():
            if n:
                print(f"    {t:<28}{n:>6}")
        boot = sum(
            1 for r in conn.execute("SELECT boot_uris FROM presets") if old in json.loads(r[0])
        )
        if boot:
            print(f"    presets.boot_uris keys{'':<13}{boot}")

    if args.dry_run:
        print("\n[dry-run] 未写库。")
        conn.close()
        return

    if args.backup:
        bak = backup(args.db)
        ok(f"已备份 → {bak}")

    # 两轮改名防交换冲突：所有旧名先挪到临时名，再从临时名落位到最终名
    try:
        conn.execute("BEGIN IMMEDIATE")
        tmp_of = {}   # old → tmp
        fin_of = {}   # old → final new
        for i, (old, new) in enumerate(pairs):
            tmp = f"__ns_migrate_tmp_{i}__"
            tmp_of[old] = tmp
            fin_of[old] = new
            for t, col in NS_TABLES.items():
                conn.execute(f"UPDATE {t} SET {col}=? WHERE {col}=?", (tmp, old))
        for old, tmp in tmp_of.items():
            for t, col in NS_TABLES.items():
                conn.execute(f"UPDATE {t} SET {col}=? WHERE {col}=?", (fin_of[old], tmp))
        remap_boot_uris(conn, fin_of)
        conn.commit()
    except Exception as e:
        conn.rollback()
        fail(f"事务失败已回滚: {e}")

    for old, new in pairs:
        ok(f"{old!r} → {new!r}")
    conn.close()


# ── copy-domains ──────────────────────────────────────────────────────────────

def cmd_copy_domains(args):
    domains = sorted({d.strip() for d in args.domains.split(",") if d.strip()})
    if not domains:
        fail("--domains 不能为空")

    conn = connect(args.db)
    if not ns_exists(conn, args.src):
        fail(f"源 namespace '{args.src}' 不存在")
    # dst 允许是新 namespace（此时 PK 冲突检查自然全过）

    dom_ph = ",".join("?" * len(domains))

    # PK 冲突预检查
    clash = conn.execute(
        f"""
        SELECT COUNT(*) FROM paths p WHERE p.namespace=? AND p.domain IN ({dom_ph})
        AND EXISTS (SELECT 1 FROM paths q WHERE q.namespace=? AND q.domain=p.domain AND q.path=p.path)
        """,
        (args.src, *domains, args.dst),
    ).fetchone()[0]
    if clash:
        fail(f"目标 namespace 已有 {clash} 条同 domain+path 路径，先处理冲突再拷贝")

    # 源节点集合：挂有目标域路径的节点
    src_nodes = [
        r[0]
        for r in conn.execute(
            f"SELECT DISTINCT node_uuid FROM paths WHERE namespace=? AND domain IN ({dom_ph})",
            (args.src, *domains),
        )
    ]
    if not src_nodes:
        fail(f"{args.src} 无 {args.domains} 数据")
    node_ph = ",".join("?" * len(src_nodes))

    # 闭包检查：源节点若同时挂有 src 中未被拷贝的域，深拷贝会导致节点
    # 分裂成两个实例（每个只带部分路径）——除非用户明确接受
    other = [
        r[0]
        for r in conn.execute(
            f"""
            SELECT DISTINCT domain FROM paths
            WHERE namespace=? AND node_uuid IN ({node_ph}) AND domain NOT IN ({dom_ph})
            """,
            (args.src, *src_nodes, *domains),
        )
    ]
    if other and not args.allow_cross_domain:
        fail(
            "源节点同时挂有未拷贝的域: " + ", ".join(other)
            + "（深拷贝会分裂成两个实例）。确认无碍请加 --allow-cross-domain"
        )

    # 拉取源数据（paths/docs/fts/emb 均只取目标域行；节点/记忆/边取全量）
    src_node_rows = conn.execute(
        f"SELECT * FROM nodes WHERE uuid IN ({node_ph})", src_nodes
    ).fetchall()
    src_mem_rows = conn.execute(
        f"SELECT * FROM memories WHERE node_uuid IN ({node_ph}) ORDER BY id", src_nodes
    ).fetchall()
    src_edge_rows = conn.execute(
        f"SELECT * FROM edges WHERE child_uuid IN ({node_ph}) ORDER BY id", src_nodes
    ).fetchall()
    src_path_rows = conn.execute(
        f"SELECT * FROM paths WHERE namespace=? AND node_uuid IN ({node_ph}) AND domain IN ({dom_ph})",
        (args.src, *src_nodes, *domains),
    ).fetchall()
    src_doc_rows = conn.execute(
        f"SELECT * FROM search_documents WHERE namespace=? AND node_uuid IN ({node_ph}) AND domain IN ({dom_ph})",
        (args.src, *src_nodes, *domains),
    ).fetchall()
    src_fts_rows = conn.execute(
        f"SELECT * FROM search_documents_fts WHERE namespace=? AND node_uuid IN ({node_ph}) AND domain IN ({dom_ph})",
        (args.src, *src_nodes, *domains),
    ).fetchall()
    want_paths = {(r["domain"], r["path"]) for r in src_path_rows}
    src_emb_rows = [
        r
        for r in conn.execute(
            "SELECT * FROM search_document_embeddings WHERE namespace=?", (args.src,)
        )
        if (r["domain"], r["path"]) in want_paths
    ]
    src_gloss_rows = [
        r
        for r in conn.execute(
            f"SELECT * FROM glossary_keywords WHERE namespace=? AND node_uuid IN ({node_ph})",
            (args.src, *src_nodes),
        )
    ]
    # glossary 只拷挂在目标域节点上的（全局表 node 映射后一并带过去）
    tgt_nodes = {r["node_uuid"] for r in src_path_rows}
    src_gloss_rows = [r for r in src_gloss_rows if r["node_uuid"] in tgt_nodes]

    plan = {
        "nodes": len(src_node_rows),
        "memories": len(src_mem_rows),
        "edges": len(src_edge_rows),
        "paths": len(src_path_rows),
        "search_documents": len(src_doc_rows),
        "fts": len(src_fts_rows),
        "glossary": len(src_gloss_rows),
        "embeddings": len(src_emb_rows),
    }
    print(f"拷贝计划 {args.src!r} → {args.dst!r}  domains={domains}:")
    for k, v in plan.items():
        print(f"  {k:<20}{v:>6}")
    print("  （nodes/edges/memories 为全局表，深拷贝为新 UUID；revisions 为审计日志不拷；")
    print("    memory_access_logs 默认不拷，需要时加 --with-access-logs）")

    if args.dry_run:
        print("\n[dry-run] 未写库。")
        conn.close()
        return

    if args.backup:
        bak = backup(args.db)
        ok(f"已备份 → {bak}")

    node_map = {r["uuid"]: str(uuid.uuid4()) for r in src_node_rows}  # old → new uuid
    mem_map = {}    # old memory id → new id
    edge_map = {}   # old edge id → new id

    try:
        conn.execute("BEGIN IMMEDIATE")

        # 1. nodes（last_accessed_at 归零：新节点无访问历史）
        for r in src_node_rows:
            conn.execute(
                "INSERT INTO nodes (uuid, created_at, last_accessed_at) VALUES (?,?,NULL)",
                (node_map[r["uuid"]], r["created_at"]),
            )

        # 2. memories（migrated_to 先置 NULL 再重映射，避免前向引用临时值）
        for r in src_mem_rows:
            cur = conn.execute(
                "INSERT INTO memories (content, deprecated, migrated_to, created_at, node_uuid, world_timestamp) "
                "VALUES (?,?,?,?,?,?)",
                (r["content"], r["deprecated"], None, r["created_at"],
                 node_map[r["node_uuid"]], r["world_timestamp"]),
            )
            mem_map[r["id"]] = cur.lastrowid
        for r in src_mem_rows:
            if r["migrated_to"] is not None:
                tgt = mem_map.get(r["migrated_to"])
                if tgt is None:
                    print(f"  ⚠ memory {r['id']} 的 migrated_to={r['migrated_to']} 不在拷贝集合内，置 NULL")
                conn.execute("UPDATE memories SET migrated_to=? WHERE id=?",
                             (tgt, mem_map[r["id"]]))

        # 3. edges（parent 在集合内 → 重映射；ROOT → 原样；集合外 → 防御性失败）
        for r in src_edge_rows:
            p = r["parent_uuid"]
            if p == ROOT_UUID:
                new_p = p
            elif p in node_map:
                new_p = node_map[p]
            else:
                raise RuntimeError(f"edge {r['id']} 的 parent {p} 不在拷贝集合内（闭包检查应已拦截）")
            cur = conn.execute(
                "INSERT INTO edges (parent_uuid, child_uuid, name, priority, disclosure, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (new_p, node_map[r["child_uuid"]], r["name"], r["priority"],
                 r["disclosure"], r["created_at"]),
            )
            edge_map[r["id"]] = cur.lastrowid

        # 4. paths
        for r in src_path_rows:
            if r["edge_id"] is not None and r["edge_id"] not in edge_map:
                raise RuntimeError(
                    f"path {r['domain']}://{r['path']} 的 edge_id={r['edge_id']} 不在拷贝的边集合内（数据异常）"
                )
            conn.execute(
                "INSERT INTO paths (namespace, domain, path, edge_id, created_at, node_uuid) "
                "VALUES (?,?,?,?,?,?)",
                (args.dst, r["domain"], r["path"], edge_map.get(r["edge_id"]),
                 r["created_at"], node_map[r["node_uuid"]]),
            )

        # 5. glossary
        for r in src_gloss_rows:
            conn.execute(
                "INSERT INTO glossary_keywords (keyword, node_uuid, namespace, created_at) VALUES (?,?,?,?)",
                (r["keyword"], node_map[r["node_uuid"]], args.dst, r["created_at"]),
            )

        # 6. search_documents（uri 与 search_terms 为纯派生值，源不变则直接复制）
        for r in src_doc_rows:
            if r["memory_id"] not in mem_map:
                raise RuntimeError(
                    f"doc {r['domain']}://{r['path']} 的 memory_id={r['memory_id']} 不在拷贝的记忆集合内"
                )
            conn.execute(
                "INSERT INTO search_documents (namespace, domain, path, node_uuid, memory_id, uri, "
                "content, disclosure, search_terms, priority, updated_at, world_timestamp) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (args.dst, r["domain"], r["path"], node_map[r["node_uuid"]],
                 mem_map[r["memory_id"]], r["uri"], r["content"], r["disclosure"],
                 r["search_terms"], r["priority"], r["updated_at"], r["world_timestamp"]),
            )

        # 7. fts（与后端写入格式一致：disclosure 用 coalesce 空串）
        for r in src_fts_rows:
            conn.execute(
                f"INSERT INTO search_documents_fts ({FTS_COLUMNS}) VALUES (?,?,?,?,?,?,?,?)",
                (args.dst, r["domain"], r["path"], node_map[r["node_uuid"]], r["uri"],
                 r["content"], r["disclosure"] or "", r["search_terms"]),
            )

        # 8. embeddings（内容未变 → vector 原样有效）
        for r in src_emb_rows:
            conn.execute(
                "INSERT INTO search_document_embeddings (namespace, domain, path, seg_index, "
                "content_hash, vector, model, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (args.dst, r["domain"], r["path"], r["seg_index"], r["content_hash"],
                 r["vector"], r["model"], r["updated_at"]),
            )

        # 9. access logs（可选）
        if args.with_access_logs:
            for r in conn.execute(
                f"SELECT * FROM memory_access_logs WHERE namespace=? AND node_uuid IN ({node_ph})",
                (args.src, *src_nodes),
            ).fetchall():
                conn.execute(
                    "INSERT INTO memory_access_logs (node_uuid, namespace, accessed_at, context) "
                    "VALUES (?,?,?,?)",
                    (node_map[r["node_uuid"]], args.dst, r["accessed_at"], r["context"]),
                )

        conn.commit()
    except Exception as e:
        conn.rollback()
        fail(f"事务失败已回滚: {e}")

    for k, v in plan.items():
        ok(f"{k}: {v} 行")
    conn.close()


# ── verify ────────────────────────────────────────────────────────────────────

def cmd_verify(args):
    conn = connect(args.db)
    problems = []

    def check(name: str, bad_count: int, detail: str = ""):
        if bad_count:
            problems.append(f"{name}: {bad_count} 处异常 {detail}")

    # 1. uri 规范：uri == domain://path
    n = conn.execute(
        "SELECT COUNT(*) FROM search_documents WHERE uri != domain || '://' || path"
    ).fetchone()[0]
    check("uri 与 domain://path 不一致", n)

    # 2. fts ↔ search_documents 完全对齐
    diff = conn.execute(
        "SELECT (SELECT COUNT(*) FROM search_documents) - (SELECT COUNT(*) FROM search_documents_fts)"
    ).fetchone()[0]
    if diff != 0:
        check("search_documents 与 fts 行数不一致", abs(diff), f"(diff={diff})")
    n = conn.execute(
        """
        SELECT COUNT(*) FROM search_documents s
        WHERE NOT EXISTS (SELECT 1 FROM search_documents_fts f
            WHERE f.namespace=s.namespace AND f.domain=s.domain AND f.path=s.path
              AND f.node_uuid=s.node_uuid AND f.uri=s.uri AND f.content=s.content)
        """
    ).fetchone()[0]
    check("fts 行内容与 search_documents 不一致", n)

    # 3. paths ↔ search_documents 对齐
    n = conn.execute(
        """
        SELECT COUNT(*) FROM paths p WHERE NOT EXISTS (SELECT 1 FROM search_documents s
            WHERE s.namespace=p.namespace AND s.domain=p.domain AND s.path=p.path
              AND s.node_uuid=p.node_uuid)
        """
    ).fetchone()[0]
    check("paths 行缺少对应 search_documents", n)
    n = conn.execute(
        """
        SELECT COUNT(*) FROM search_documents s WHERE NOT EXISTS (SELECT 1 FROM paths p
            WHERE p.namespace=s.namespace AND p.domain=s.domain AND p.path=s.path
              AND p.node_uuid=s.node_uuid)
        """
    ).fetchone()[0]
    check("search_documents 行缺少对应 paths", n)

    # 4. FK 完整性: (表, 引用列, 目标表, 目标主键列)
    for t, col, tgt, tgt_col in [
        ("paths", "node_uuid", "nodes", "uuid"),
        ("paths", "edge_id", "edges", "id"),
        ("memories", "node_uuid", "nodes", "uuid"),
        ("memories", "migrated_to", "memories", "id"),
        ("search_documents", "node_uuid", "nodes", "uuid"),
        ("search_documents", "memory_id", "memories", "id"),
        ("glossary_keywords", "node_uuid", "nodes", "uuid"),
        ("search_documents_fts", "node_uuid", "nodes", "uuid"),
    ]:
        n = conn.execute(
            f"SELECT COUNT(*) FROM {t} t1 WHERE t1.{col} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {tgt} t2 WHERE t2.{tgt_col} = t1.{col})"
        ).fetchone()[0]
        check(f"{t}.{col} 悬空引用", n)

    # 5. embeddings PK 对应真实 doc 行
    n = conn.execute(
        """
        SELECT COUNT(*) FROM search_document_embeddings e WHERE NOT EXISTS
            (SELECT 1 FROM search_documents s WHERE s.namespace=e.namespace
             AND s.domain=e.domain AND s.path=e.path)
        """
    ).fetchone()[0]
    check("embeddings 指向不存在的 search_documents", n)

    # 6. boot_uris keys 有效性：有数据的 key 必须存在
    for p in conn.execute("SELECT id, name, boot_uris FROM presets").fetchall():
        for k in json.loads(p["boot_uris"]):
            if k == "":
                continue
            if not ns_exists(conn, k):
                problems.append(f"preset#{p['id']}({p['name']}) boot_uris key '{k}' 无对应 namespace 数据")

    # 7. 同节点多条 active memory（UNIQUE 索引兜底，此处复核）
    n = conn.execute(
        """
        SELECT COUNT(*) FROM (SELECT node_uuid FROM memories
            WHERE deprecated=0 AND node_uuid IS NOT NULL
            GROUP BY node_uuid HAVING COUNT(*) > 1)
        """
    ).fetchone()[0]
    check("同节点多条 active memory", n)

    # 8. 孤儿边：child 有路径而 parent 无路径（且非 ROOT）
    n = conn.execute(
        f"""
        SELECT COUNT(*) FROM edges e
        WHERE e.parent_uuid != '{ROOT_UUID}'
          AND EXISTS (SELECT 1 FROM paths p WHERE p.node_uuid=e.child_uuid)
          AND NOT EXISTS (SELECT 1 FROM paths p WHERE p.node_uuid=e.parent_uuid)
        """
    ).fetchone()[0]
    check("edge 父节点无任何路径（孤儿边）", n)

    if problems:
        print(f"✗ 发现 {len(problems)} 类问题:")
        for p in problems:
            print(f"  - {p}")
        conn.close()
        sys.exit(1)
    ok("全库一致性校验通过：uri 规范、派生表对齐、FK 完整、boot_uris keys 有效")
    conn.close()


# ── 主入口 ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="ns_migrate.py",
        description="nocturne-memory SQLite 库跨 namespace 迁移工具",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stats", help="namespace × domain 分布概览")
    p.add_argument("db")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("rename", help="原子重命名/交换 namespace")
    p.add_argument("db")
    p.add_argument("--old", help="旧名（单对模式）")
    p.add_argument("--new", help="新名（单对模式）")
    p.add_argument("--swap", nargs="+", metavar="OLD:NEW",
                   help="多对交换/链式重命名，如 a:b b:a")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--backup", action="store_true", help="执行前先 .backup 在线备份")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("copy-domains", help="深拷贝 namespace 的指定 domain 到另一 namespace")
    p.add_argument("db")
    p.add_argument("src", help="源 namespace")
    p.add_argument("dst", help="目标 namespace（可不存在）")
    p.add_argument("--domains", required=True,
                   help="逗号分隔，如 diary,history,history_raw,maintenance")
    p.add_argument("--allow-cross-domain", action="store_true",
                   help="源节点同时挂未拷贝域时仍继续（节点深拷贝分裂成两实例）")
    p.add_argument("--with-access-logs", action="store_true", help="连同 memory_access_logs 一起拷")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--backup", action="store_true", help="执行前先 .backup 在线备份")
    p.set_defaults(func=cmd_copy_domains)

    p = sub.add_parser("verify", help="全库一致性校验")
    p.add_argument("db")
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
