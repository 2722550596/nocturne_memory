#!/usr/bin/env python3
"""
scripts/migrate_all_databases.py — 把所有 Nocturne SQLite 库迁移到最新 schema

扫描仓库内所有 *.db 文件（排除 .bak / -wal / -shm / 空文件），对每个库执行
init_db()：补建缺失的表并应用所有 pending migrations（含 017 语义向量表
search_document_embeddings）。有 pending migration 的库会自动先备份
（xxx.db.<时间戳>.bak，迁移机制内置行为）。

可选 --backfill：迁移后为每个库回填语义搜索向量。会真实调用 embedding API
（需配置 NOCTURNE_EMBEDDING_API_KEY）；已 hash 匹配的段自动跳过，幂等，
可重复运行。

用法：
  python scripts/migrate_all_databases.py                     # 迁移所有库
  python scripts/migrate_all_databases.py --db data/stoneford.db
  python scripts/migrate_all_databases.py --backfill          # 迁移 + 向量回填
  python scripts/migrate_all_databases.py --dry-run           # 只预览，不改动
"""

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def find_databases(root: Path) -> list:
    """Find every usable SQLite db under *root*, skipping backups/WAL/empty."""
    dbs = []
    for path in sorted(root.rglob("*.db")):
        name = path.name
        if name.endswith(".bak") or "-wal" in name or "-shm" in name or name.endswith(".bak.db"):
            continue
        if path.stat().st_size == 0:
            continue
        dbs.append(path)
    return dbs


def read_state(path: Path) -> dict:
    """Read-only schema state via plain sqlite3 (no side effects)."""
    state = {
        "path": path,
        "size_kb": path.stat().st_size // 1024,
        "initialized": False,
        "migrations": [],
        "has_embeddings_table": False,
        "embeddings_rows": 0,
        "search_docs": 0,
        "namespaces": [],
    }
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in cur.fetchall()}
            if "memories" not in tables:
                return state  # not an initialized nocturne db
            state["initialized"] = True
            if "schema_migrations" in tables:
                cur.execute("SELECT version FROM schema_migrations ORDER BY version")
                state["migrations"] = [r[0] for r in cur.fetchall()]
            state["has_embeddings_table"] = "search_document_embeddings" in tables
            if state["has_embeddings_table"]:
                cur.execute("SELECT COUNT(*) FROM search_document_embeddings")
                state["embeddings_rows"] = cur.fetchone()[0]
            if "search_documents" in tables:
                cur.execute("SELECT COUNT(*) FROM search_documents")
                state["search_docs"] = cur.fetchone()[0]
                cur.execute("SELECT DISTINCT namespace FROM search_documents")
                state["namespaces"] = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    except sqlite3.Error as e:
        state["error"] = str(e)
    return state


def pending_migrations(state: dict) -> list:
    """Migration files not yet applied to this db (from the migrations dir)."""
    migrations_dir = BACKEND_DIR / "db" / "migrations"
    files = sorted(
        f.name for f in migrations_dir.glob("*.py") if f.name[0].isdigit()
    )
    applied = set(state.get("migrations", []))
    return [f for f in files if f not in applied]


async def migrate_one(path: Path) -> dict:
    """Apply pending migrations via the real init_db path."""
    from db.database import DatabaseManager

    url = f"sqlite+aiosqlite:///{path.resolve().as_posix()}"
    manager = DatabaseManager(url)
    try:
        await manager.init_db()
    finally:
        await manager.close()
    return read_state(path)


async def backfill_one(path: Path, state: dict) -> dict:
    """Backfill semantic-search embeddings for every namespace in *db*.

    Real embedding API calls; skips segments whose content_hash already
    matches (idempotent).  Returns per-namespace counts.
    """
    from db.database import DatabaseManager
    from db.embeddings import EmbeddingService
    from sqlalchemy import text

    url = f"sqlite+aiosqlite:///{path.resolve().as_posix()}"
    manager = DatabaseManager(url)
    service = EmbeddingService(manager)
    report = {"skipped": False, "namespaces": {}}
    if not service.enabled():
        report["skipped"] = True
        await manager.close()
        return report

    try:
        async with manager.session() as session:
            for ns in state.get("namespaces", []):
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT namespace, domain, path, uri, content,
                                   disclosure, search_terms
                            FROM search_documents
                            WHERE namespace = :ns
                            """
                        ),
                        {"ns": ns},
                    )
                ).all()
                docs = [
                    {
                        "namespace": r.namespace,
                        "domain": r.domain,
                        "path": r.path,
                        "uri": r.uri,
                        "content": r.content,
                        "disclosure": r.disclosure,
                        "search_terms": r.search_terms,
                    }
                    for r in rows
                ]
                before = (
                    await session.execute(
                        text("SELECT COUNT(*) FROM search_document_embeddings")
                    )
                ).scalar_one()
                if docs:
                    await service.ensure_documents_embeddings(docs)
                after = (
                    await session.execute(
                        text("SELECT COUNT(*) FROM search_document_embeddings")
                    )
                ).scalar_one()
                report["namespaces"][ns] = {
                    "docs": len(docs),
                    "vectors_before": before,
                    "vectors_after": after,
                }
    finally:
        await manager.close()
    return report


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate all Nocturne SQLite databases to the latest schema."
    )
    parser.add_argument("--db", action="append", default=None, metavar="PATH",
                        help="Only migrate this db (repeatable); default: all found")
    parser.add_argument("--backfill", action="store_true",
                        help="Also backfill semantic-search embeddings (real API calls)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only report state; do not modify anything")
    args = parser.parse_args(argv)

    if args.db:
        targets = [Path(p).resolve() for p in args.db]
    else:
        targets = find_databases(REPO_ROOT)

    print(f"发现 {len(targets)} 个数据库文件：\n")
    results = []
    for path in targets:
        state = read_state(path)
        if not state.get("initialized"):
            print(f"  ⏭  {path.relative_to(REPO_ROOT)}  (未初始化的 nocturne 库，跳过)")
            continue
        pending = pending_migrations(state)
        flag = "已是最新" if not pending else f"待迁移 {len(pending)} 个"
        size = state["size_kb"]
        emb = f"，向量 {state['embeddings_rows']} 行" if state["has_embeddings_table"] else ""
        print(f"  • {path.relative_to(REPO_ROOT)}  ({size} KB, {flag}{emb})")
        if pending:
            print(f"      pending: {', '.join(pending)}")
        results.append((path, state, pending))

    if args.dry_run:
        print("\n[dry-run] 未做任何修改。")
        return 0

    print()
    for path, state, pending in results:
        rel = path.relative_to(REPO_ROOT)
        if not pending:
            print(f"[跳过] {rel} — 已是最新 schema")
            continue
        print(f"[迁移] {rel} …")
        new_state = await migrate_one(path)
        applied = [f for f in pending if f in new_state.get("migrations", [])]
        emb = new_state["embeddings_rows"] if new_state["has_embeddings_table"] else 0
        print(f"       ✓ 应用 {len(applied)} 个迁移；向量表行数 {emb}")

    if args.backfill:
        print("\n[回填] 语义向量（真实 embedding API）…")
        for path, state, _pending in results:
            rel = path.relative_to(REPO_ROOT)
            fresh = read_state(path)
            report = await backfill_one(path, fresh)
            if report.get("skipped"):
                print(f"  ⏭  {rel} — 未配置 NOCTURNE_EMBEDDING_API_KEY，跳过")
                continue
            parts = [f"{ns}: {v['docs']} 文档 → {v['vectors_after'] - v['vectors_before']:+d} 段"
                     for ns, v in report["namespaces"].items()]
            print(f"  ✓ {rel} — " + ("；".join(parts) if parts else "无文档"))
    else:
        print("\n提示：加 --backfill 可同时回填语义向量（需 embedding API key）。")

    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
