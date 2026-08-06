"""
Review API — Grouped by node_uuid, with universal rollback.

This module provides endpoints for reviewing and rolling back changes made by the AI.
Unlike traditional systems that track operations (e.g., "create", "update"), this system 
groups physical row changes (nodes, memories, edges, paths) by their top-level `node_uuid`.
This allows a "universal rollback" that dynamically inspects the `before` state of the 
data and reverts it exactly as it was, eliminating edge cases from compound AI actions.
"""
from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any, Optional

from locales import t
from models import (
    DiffRequest, DiffResponse,
    ChangeGroup, UriDiff,
    GroupRollbackResponse,
)
from .utils import get_text_diff
from db.snapshot import (
    get_changeset_store, _make_row_key,
    commit_revision, get_revision_tree, get_revision, is_ancestor,
)
from db import get_graph_service, get_search_indexer, get_db_manager

router = APIRouter(prefix="/review", tags=["review"])


def _resolve_node_uuid_sync(row: Dict[str, Any], all_rows: List[Dict[str, Any]], db_edge_to_node: Dict[int, str]) -> Optional[str]:
    """
    Resolve the top-level node_uuid for a given changeset row.
    
    Since changes are recorded at the row level across 4 tables (nodes, memories, edges, paths),
    this function traces foreign keys up the hierarchy to find the root node_uuid.
    
    Args:
        row: The changeset row dict (containing table, before, and after states).
        all_rows: All current rows in the changeset, used to trace path -> edge -> node.
        db_edge_to_node: A pre-fetched cache mapping edge_id to child_uuid for edges 
                         that exist in the live DB but aren't in the changeset.
                         
    Returns:
        The resolved node_uuid as a string, or None if unresolvable.
    """
    table = row["table"]
    # Use the 'before' state if it exists, otherwise fallback to 'after' state
    ref = row["before"] if row["before"] else row["after"]
    if not ref:
        return None
        
    if table == "nodes":
        return ref.get("uuid")
    if table == "memories":
        return ref.get("node_uuid")
    if table == "glossary_keywords":
        # Glossary bindings belong to the node they annotate only.
        # They are not part of the graph topology, so review grouping
        # intentionally does not fold them into parent/child cascades.
        return ref.get("node_uuid")
    if table == "edges":
        return ref.get("child_uuid")
    if table == "paths":
        # Some paths (root paths) map directly to a node_uuid
        if ref.get("node_uuid"):
            return ref.get("node_uuid")
            
        edge_id = ref.get("edge_id")
        if edge_id is not None:
            # 1. Try to resolve edge_id -> child_uuid from within the changeset itself
            for r in all_rows:
                if r["table"] == "edges":
                    eref = r["before"] if r["before"] else r["after"]
                    if eref and eref.get("id") == edge_id and eref.get("child_uuid"):
                        return eref.get("child_uuid")
            # 2. If not in changeset, fallback to the pre-fetched live DB cache
            return db_edge_to_node.get(edge_id)
    return None


def _get_causal_anchors(changed_rows: List[Dict[str, Any]], all_rows: List[Dict[str, Any]], db_edge_to_node: Dict[int, str]) -> Dict[str, str]:
    """
    计算每一行变更的“因果锚点 (Causal Anchor)”。
    
    由于底层是细粒度的行级记录（nodes, memories, edges, paths），
    AI 的一个高层操作（如：删除某节点）往往会引发一连串的级联变动
    （如：它的子路径全部被删、连向它的边被废弃、它的记忆被 GC 废弃等）。
    
    如果仅仅按照每一行自身外键指向的 `node_uuid` 来分组，会导致高层操作被
    “撕裂”成好几个零碎的审核组，不仅增加审核负担，更会给用户留下
    “只回滚了一半导致数据库外键结构断裂”的隐患。
    
    本函数的作用，就是在快照记录中逆向追踪这种“级联关系”：
    如果记录 B 的发生是因为记录 A 导致的，那么 B 就会认 A 为“父因”，
    并最终把 B 的变动“折叠（归属）”到 A 所属的最高层节点上去。
    """
    anchors = {}
    row_by_key = {}
    
    paths_by_string = {}
    incoming_edges = {}
    
    # =========================================================================
    # 步骤 1：初步归属与索引构建 (Initial Assignment & Indexing)
    # 遍历所有被改变的行，先计算出它自身“字面上”属于哪个 node_uuid。
    # 同时建立快速查询字典，方便后续查找某条 path 或是某个节点的入边 (incoming edges)。
    # =========================================================================
    for row in changed_rows:
        key = _make_row_key(row["table"], row["before"] if row["before"] else row["after"])
        row_by_key[key] = row
        
        # 自身字面上的节点归属（比如 edge A->B，字面上属于 B）
        node_uuid = _resolve_node_uuid_sync(row, all_rows, db_edge_to_node)
        anchors[key] = node_uuid
        
        # 将 paths 按照 URI 字符串索引起来，方便查它的父级 path
        if row["table"] == "paths":
            ref = row["before"] if row["before"] else row["after"]
            ns = ref.get("namespace", "")
            paths_by_string[f"{ns}::{ref['domain']}://{ref['path']}"] = row
            
        # 将 edges 按照它指向的 child_uuid 索引起来，找“谁连向了我”
        elif row["table"] == "edges":
            ref = row["before"] if row["before"] else row["after"]
            incoming_edges.setdefault(ref["child_uuid"], []).append(row)

    # =========================================================================
    # 步骤 2：构建因果指针网络 (Build Causal Parent Pointers)
    # 为每一行数据寻找“导致它发生变动”的直接原因行，构建 parent_map。
    # =========================================================================
    parent_map = {}
    
    # 辅助函数：判断两个操作是否是同一个方向的同类操作（要么都是被删，要么都是新建）
    # 比如：父节点被删，子节点也被删，这叫方向一致，构成因果。
    def is_deleted(r): return r["before"] is not None and r["after"] is None
    def is_created(r): return r["before"] is None and r["after"] is not None
    def same_action(r1, r2):
        if is_deleted(r1) and is_deleted(r2): return True
        if is_created(r1) and is_created(r2): return True
        return False

    for key, row in row_by_key.items():
        if row["table"] == "paths":
            # 场景 A：路径级联 (Path Cascades)
            # 比如创建/删除 core://A/B 时，检查快照里有没有创建/删除 core://A
            # 如果有，说明 B 的变化是被 A 顺带引发的。B 认 A 为父因。
            # 但是！如果这个 path 对应的 edge 在这次快照里也是同步新建/删除的，
            # 那么说明这是一个完全独立的节点创建/删除，而不只是父目录移动造成的从属路径级联。
            # 此时不应该将它折叠到父路径中，而应该让它作为独立的组展示。
            ref = row["before"] if row["before"] else row["after"]
            
            edge_changed_in_sync = False
            edge_id = ref.get("edge_id")
            if edge_id is not None:
                for r in changed_rows:
                    if r["table"] == "edges":
                        eref = r["before"] if r["before"] else r["after"]
                        if eref and eref.get("id") == edge_id and same_action(row, r):
                            edge_changed_in_sync = True
                            break

            if "/" in ref["path"]:
                parent_path = ref["path"].rsplit("/", 1)[0]
                ns = ref.get("namespace", "")
                parent_uri = f"{ns}::{ref['domain']}://{parent_path}"
                parent_row = paths_by_string.get(parent_uri)
                if parent_row and same_action(row, parent_row):
                    pref = parent_row["before"] if parent_row["before"] else parent_row["after"]
                    
                    is_independent = False
                    if edge_changed_in_sync:
                        if is_deleted(row):
                            # 对于删除：检查是否是父节点级联删除引发的
                            parent_edge_deleted = False
                            p_edge_id = pref.get("edge_id")
                            if p_edge_id is not None:
                                for r in changed_rows:
                                    if r["table"] == "edges":
                                        eref = r["before"] if r["before"] else r["after"]
                                        if eref and eref.get("id") == p_edge_id and same_action(parent_row, r):
                                            parent_edge_deleted = True
                                            break
                            else:
                                # 如果父路径没有 edge_id（即它是根路径），且它正在被删除，
                                # 则默认这是一个级联删除，不应视为独立删除。
                                parent_edge_deleted = True
                                
                            # 如果父节点 edge 没被删，说明当前节点是独立删除，不应折叠
                            if not parent_edge_deleted:
                                is_independent = True
                        elif is_created(row):
                            # 对于创建：没有级联创建子树 edge 的逻辑。
                            # 只要当前节点的 edge 是新建的，它就是独立创建的，不应折叠到父 path。
                            # （子树移动时，子节点的 edge 不会新建，edge_changed_in_sync 为 False，依然会正常折叠）
                            is_independent = True

                    if not is_independent:
                        parent_map[key] = _make_row_key("paths", pref)
                    
        elif row["table"] == "edges":
            # 场景 B：边的级联删除/创建 (Edge Cascades)
            # 一条边为什么会变？
            ref = row["before"] if row["before"] else row["after"]
            
            # 第一种可能：是因为绑在它身上的 path 被删了/新建了。
            found_path = False
            for p_uri, p_row in paths_by_string.items():
                p_ref = p_row["before"] if p_row["before"] else p_row["after"]
                if p_ref.get("edge_id") == ref["id"] and same_action(row, p_row):
                    parent_map[key] = _make_row_key("paths", p_ref)
                    found_path = True
                    break
            
            # 第二种可能：如果是纯粹的结构性断裂（连 path 都没有），那是因为它的父节点被删了。
            # 这时它去寻找连向它父节点（parent_uuid）的边是否也被删了。
            if not found_path:
                inc_edges = incoming_edges.get(ref["parent_uuid"], [])
                for inc in inc_edges:
                    if same_action(row, inc):
                        parent_map[key] = _make_row_key("edges", inc["before"] if inc["before"] else inc["after"])
                        break
                        
        elif row["table"] == "memories":
            # 场景 C：记忆的垃圾回收 (Memory GC)
            # 如果记忆被删除了（物理删除或废弃且无替代者），大概率是被 GC 的。
            ref_b = row["before"]
            ref_a = row["after"]
            is_gc = False
            
            if is_deleted(row):
                is_gc = True
            elif ref_b and not ref_b.get("deprecated") and ref_a and ref_a.get("deprecated"):
                # 检查是否只是常规的 update (有新的非废弃记忆补上)
                node_uuid = ref_b["node_uuid"]
                has_new_active = False
                for r in changed_rows:
                    if r["table"] == "memories":
                        r_a = r.get("after")
                        if r_a and r_a.get("node_uuid") == node_uuid and not r_a.get("deprecated") and r.get("before") is None:
                            has_new_active = True
                            break
                if not has_new_active:
                    is_gc = True
            
            # 如果确认是 GC 废弃，说明是由于节点失去所有入边引起的。
            # 将这个记忆的变更，归因到指向该节点的边的被删上。
            if is_gc:
                node_uuid = (ref_b or ref_a)["node_uuid"]
                inc_edges = incoming_edges.get(node_uuid, [])
                for inc in inc_edges:
                    if is_deleted(inc):
                        parent_map[key] = _make_row_key("edges", inc["before"] if inc["before"] else inc["after"])
                        break
                        
        elif row["table"] == "nodes":
            # 场景 D：节点的级联删除
            # 节点被物理删除，一定是因为连向它的所有边都被删了。
            ref = row["before"] if row["before"] else row["after"]
            inc_edges = incoming_edges.get(ref["uuid"], [])
            for inc in inc_edges:
                if same_action(row, inc):
                    parent_map[key] = _make_row_key("edges", inc["before"] if inc["before"] else inc["after"])
                    break

    # =========================================================================
    # 步骤 3：寻根问祖 (Resolve Roots)
    # 通过 parent_map 不断往上找，直到找到没有父节点的最顶层根因，
    # 拿这个根因的 node_uuid 作为当前行最终折叠进去的审核归属。
    # =========================================================================
    def get_root(k):
        curr = k
        visited = set()
        while curr in parent_map and curr not in visited:
            visited.add(curr)
            curr = parent_map[curr]
        return curr
        
    final_anchors = {}
    for key in row_by_key:
        root_key = get_root(key)
        # 将行强行绑定到它最顶端原因所在的那个 node_uuid 上
        final_anchors[key] = anchors[root_key]
        
    return final_anchors


TABLE_RANK = {"nodes": 5, "memories": 4, "edges": 3, "paths": 2, "glossary_keywords": 1}
RANK_TO_TABLE = {v: k for k, v in TABLE_RANK.items()}


def _determine_top_table_and_action(rows: List[Dict[str, Any]]):
    tables_present = {r["table"] for r in rows}
    top_rank = max((TABLE_RANK[t] for t in tables_present), default=1)
    top_table = RANK_TO_TABLE[top_rank]
    top_table_rows = [r for r in rows if r["table"] == top_table]
    action = "modified"
    if all(r["before"] is None and r["after"] is not None for r in top_table_rows):
        action = "created"
    elif all(r["before"] is not None and r["after"] is None for r in top_table_rows):
        action = "deleted"
    return top_table, action


class _ReviewContext:
    __slots__ = ("store", "all_rows", "changed_rows", "anchors", "db_edge_to_node")

    def __init__(self, store, all_rows, changed_rows, anchors, db_edge_to_node):
        self.store = store
        self.all_rows = all_rows
        self.changed_rows = changed_rows
        self.anchors = anchors
        self.db_edge_to_node = db_edge_to_node

    def rows_for_node(self, node_uuid: str) -> List[Dict[str, Any]]:
        rows = []
        for row in self.changed_rows:
            key = _make_row_key(row["table"], row["before"] if row["before"] else row["after"])
            if self.anchors.get(key) == node_uuid:
                rows.append(row)
        return rows

    def keys_for_node(self, node_uuid: str) -> List[str]:
        keys = []
        for row in self.all_rows:
            key = _make_row_key(row["table"], row["before"] if row["before"] else row["after"])
            if self.anchors.get(key) == node_uuid:
                keys.append(key)
            elif row not in self.changed_rows and _resolve_node_uuid_sync(row, self.all_rows, self.db_edge_to_node) == node_uuid:
                keys.append(key)
        return keys


async def _build_review_context() -> _ReviewContext:
    store = get_changeset_store()
    all_rows_dict, changed_rows = store.get_snapshot_view()
    all_rows = list(all_rows_dict.values())

    edge_ids_to_resolve = set()
    for row in changed_rows:
        if row["table"] == "paths":
            ref = row["before"] if row["before"] else row["after"]
            if ref and ref.get("edge_id"):
                edge_ids_to_resolve.add(ref["edge_id"])

    db_edge_to_node = {}
    if edge_ids_to_resolve:
        db = get_db_manager()
        from sqlalchemy import select
        from db.models import Edge
        async with db.session() as session:
            res = await session.execute(
                select(Edge.id, Edge.child_uuid).where(Edge.id.in_(edge_ids_to_resolve))
            )
            for eid, child_uuid in res:
                db_edge_to_node[eid] = child_uuid

    anchors = _get_causal_anchors(changed_rows, all_rows, db_edge_to_node)
    return _ReviewContext(store, all_rows, changed_rows, anchors, db_edge_to_node)


@router.get("/groups", response_model=List[ChangeGroup])
async def list_groups():
    """
    List all pending changes, grouped by their top-level node_uuid.
    
    Returns a summarized list of ChangeGroups for the frontend sidebar. 
    It dynamically groups rows, resolves URIs for display, and determines 
    the highest-level table affected (node > memory > edge > path).
    """
    ctx = await _build_review_context()

    groups: Dict[str, List[Dict]] = {}
    for row in ctx.changed_rows:
        key = _make_row_key(row["table"], row["before"] if row["before"] else row["after"])
        node_uuid = ctx.anchors.get(key)
        if node_uuid:
            groups.setdefault(node_uuid, []).append(row)
            
    result = []
    for node_uuid, rows in groups.items():
        top_table, action = _determine_top_table_and_action(rows)
        
        display_uri = None
        namespaces = set()
        
        for r in ctx.all_rows:
            if r["table"] == "paths":
                if _resolve_node_uuid_sync(r, ctx.all_rows, ctx.db_edge_to_node) == node_uuid:
                    pref = r["before"] if r["before"] else r["after"]
                    if pref:
                        if not display_uri:
                            display_uri = f"{pref.get('domain', 'core')}://{pref.get('path', '')}"
                        if pref.get('namespace') is not None:
                            namespaces.add(pref.get('namespace'))
        
        graph = get_graph_service()
        paths_data = await graph.get_paths_for_node(node_uuid, search_all_namespaces=True)
        for p in paths_data:
            if not display_uri:
                display_uri = f"{p['domain']}://{p['path']}"
            if p['namespace'] is not None:
                namespaces.add(p['namespace'])
                    
        if not display_uri:
            display_uri = f"[unmapped]/{node_uuid}"
            
        final_namespaces = None
        if namespaces:
            final_namespaces = sorted(list(namespaces))
            
        result.append(ChangeGroup(
            node_uuid=node_uuid,
            display_uri=display_uri,
            top_level_table=top_table,
            action=action,
            row_count=len(rows),
            namespaces=final_namespaces
        ))
        
    return sorted(result, key=lambda x: x.display_uri)


async def _extract_content_and_meta_for_node(rows: List[Dict[str, Any]], slot: str, node_uuid: str):
    """
    Extract memory content and edge metadata (priority, disclosure) for diff comparison.
    
    Memory content is huge, so it is NOT stored directly in the JSON changeset.
    Instead, the changeset stores pointers (memory_id). This function extracts
    those IDs and fetches the actual content/metadata from the live DB.
    
    Args:
        rows: List of changeset rows for a specific node_uuid.
        slot: Which state to extract ("before" or "after").
        node_uuid: The parent node_uuid.
    """
    memory_id = None
    meta = {"priority": None, "disclosure": None}
    
    # 1. Look for memory and edge metadata inside the provided rows
    for row in rows:
        data = row.get(slot)
        if data is None:
            continue
        # Find the active (non-deprecated) memory ID
        if row["table"] == "memories" and not data.get("deprecated", False):
            memory_id = data.get("id")
        # Find edge metadata
        elif row["table"] == "edges":
            meta["priority"] = data.get("priority")
            meta["disclosure"] = data.get("disclosure")

    content = None
    graph = get_graph_service()
    db = get_db_manager()
    
    node_created = any(r["table"] == "nodes" and r["before"] is None and (r.get("after") or {}).get("uuid") == node_uuid for r in rows)
    node_deleted = any(r["table"] == "nodes" and r["after"] is None and (r.get("before") or {}).get("uuid") == node_uuid for r in rows)

    if memory_id is not None:
        # If we found a memory pointer in the changeset, fetch its content from DB
        mem = await graph.get_memory_by_id(memory_id)
        if mem:
            content = mem.get("content")
    else:
        # 2. Fallback: If no memory pointer was in the changeset (e.g. only paths changed),
        # query the live DB for the currently active memory and edge of this node.
        memory_created = any(r["table"] == "memories" and r["before"] is None and (r.get("after") or {}).get("node_uuid") == node_uuid for r in rows)
        memory_deleted = any(r["table"] == "memories" and r["after"] is None and (r.get("before") or {}).get("node_uuid") == node_uuid for r in rows)
        should_fetch_memory = True
        
        # If the memory or node was created in this changeset, it has no 'before' state.
        # Don't fetch the current live DB state and incorrectly present it as 'before'.
        if slot == "before" and (node_created or memory_created):
            should_fetch_memory = False
            
        if slot == "after" and (node_deleted or memory_deleted):
            should_fetch_memory = False
            
        if should_fetch_memory:
            from sqlalchemy import select
            from db.models import Memory
            async with db.session() as session:
                mem = (await session.execute(
                    select(Memory).where(Memory.node_uuid == node_uuid, Memory.deprecated == False)
                )).scalar_one_or_none()
                if mem:
                    content = mem.content

    if meta["priority"] is None:
        edge_created = any(r["table"] == "edges" and r["before"] is None and (r.get("after") or {}).get("child_uuid") == node_uuid for r in rows)
        edge_deleted = any(r["table"] == "edges" and r["after"] is None and (r.get("before") or {}).get("child_uuid") == node_uuid for r in rows)
        should_fetch_edge = True
        
        # If the edge or node was created in this changeset, it has no 'before' state.
        if slot == "before" and (node_created or edge_created):
            should_fetch_edge = False
            
        if slot == "after" and (node_deleted or edge_deleted):
            should_fetch_edge = False
            
        if should_fetch_edge:
            from sqlalchemy import select
            from db.models import Edge
            async with db.session() as session:
                edge = (await session.execute(select(Edge).where(Edge.child_uuid == node_uuid).limit(1))).scalar_one_or_none()
                if edge:
                    meta["priority"] = edge.priority
                    meta["disclosure"] = edge.disclosure

    return content, meta


@router.get("/groups/{node_uuid}/diff", response_model=UriDiff)
async def get_group_diff(node_uuid: str):
    """
    Compute the before vs after (current) diff for a specific node group.
    
    Used by the review UI to highlight content modifications or metadata changes.
    """
    ctx = await _build_review_context()
    db = get_db_manager()
    from sqlalchemy import select
    from db.models import Edge

    rows = ctx.rows_for_node(node_uuid)
    if not rows:
        raise HTTPException(404, t("api.review.no_changes_for_node").format(node_uuid=node_uuid))

    top_table, action = _determine_top_table_and_action(rows)

    path_changes = []
    for r in rows:
        if r["table"] == "paths":
            if r["before"] is None and r["after"] is not None:
                path_changes.append({
                    "action": "created",
                    "uri": f"{r['after']['domain']}://{r['after']['path']}",
                    "namespace": r["after"].get("namespace", "")
                })
            elif r["before"] is not None and r["after"] is None:
                path_changes.append({
                    "action": "deleted",
                    "uri": f"{r['before']['domain']}://{r['before']['path']}",
                    "namespace": r["before"].get("namespace", "")
                })
                
    glossary_changes = []
    for r in rows:
        if r["table"] == "glossary_keywords":
            if r["before"] is None and r["after"] is not None:
                glossary_changes.append({
                    "action": "created",
                    "keyword": r["after"]["keyword"]
                })
            elif r["before"] is not None and r["after"] is None:
                glossary_changes.append({
                    "action": "deleted",
                    "keyword": r["before"]["keyword"]
                })
                
    active_paths = []
    path_namespaces = {}
    node_is_deleted = (top_table == "nodes" and action == "deleted")
    if not node_is_deleted:
        graph = get_graph_service()
        paths_data = await graph.get_paths_for_node(node_uuid, search_all_namespaces=True)
        
        uri_to_ns = {}
        for p in paths_data:
            uri_str = f"{p['domain']}://{p['path']}"
            ns = p['namespace'] or ""
            if uri_str not in uri_to_ns:
                uri_to_ns[uri_str] = set()
            uri_to_ns[uri_str].add(ns)
                
        for uri_str, ns_set in uri_to_ns.items():
            active_paths.append(uri_str)
            path_namespaces[uri_str] = sorted(list(ns_set))

    before_content, before_meta = await _extract_content_and_meta_for_node(rows, "before", node_uuid)
    current_content, current_meta = await _extract_content_and_meta_for_node(rows, "after", node_uuid)
    
    # If the "after" state wasn't fully captured in the snapshot (e.g. partial edits),
    # use the actual live DB state as the absolute ground truth for current data.
    if current_content is None and current_meta["priority"] is None:
        node_deleted = any(r["table"] == "nodes" and r["after"] is None and (r.get("before") or {}).get("uuid") == node_uuid for r in rows)
        edge_deleted = any(r["table"] == "edges" and r["after"] is None and (r.get("before") or {}).get("child_uuid") == node_uuid for r in rows)
        
        async with db.session() as session:
            from db.models import Memory
            mem = (await session.execute(
                select(Memory).where(Memory.node_uuid == node_uuid, Memory.deprecated == False)
            )).scalar_one_or_none()
            if mem:
                current_content = mem.content
                
            if not (node_deleted or edge_deleted):
                edge = (await session.execute(select(Edge).where(Edge.child_uuid == node_uuid).limit(1))).scalar_one_or_none()
                if edge:
                    current_meta["priority"] = edge.priority
                    current_meta["disclosure"] = edge.disclosure

    has_changes = (before_content != current_content) or (before_meta != current_meta) or bool(glossary_changes)

    return UriDiff(
        uri=node_uuid,
        change_type=top_table,
        action=action,
        before_content=before_content,
        current_content=current_content,
        before_meta=before_meta,
        current_meta=current_meta,
        path_changes=path_changes if path_changes else None,
        glossary_changes=glossary_changes if glossary_changes else None,
        active_paths=active_paths if active_paths else None,
        path_namespaces=path_namespaces if path_namespaces else None,
        has_changes=has_changes,
    )

async def _revert_rows_to_before(session, graph, search, rows, fallback_node_uuid=None):
    """Revert DB state for `rows` to each row's `before` snapshot.

    Shared by rollback_group (per-node) and checkout (accumulated undo path).
    Mirrors the universal rollback: cascade-delete created nodes, remove created
    paths, restore deleted paths, revert edge metadata, revive old memories,
    revert glossary keywords. Returns a list of human-readable messages.
    """
    from sqlalchemy import select, update, delete
    from db.models import Edge, GlossaryKeyword, Node

    messages = []

    node_created = any(r["table"] == "nodes" and r["before"] is None for r in rows)
    if node_created and fallback_node_uuid:
        await graph.cascade_delete_node(session, fallback_node_uuid)
        messages.append("Deleted created node and its dependencies.")
    else:
        # Revert Path Changes by sorting lengths
        path_rows = [r for r in rows if r["table"] == "paths"]
        path_creations = [r for r in path_rows if r["before"] is None and r["after"] is not None]
        path_creations.sort(key=lambda r: len(r["after"]["path"].split("/")), reverse=True)  # remove children first
        path_deletions = [r for r in path_rows if r["before"] is not None and r["after"] is None]
        path_deletions.sort(key=lambda r: len(r["before"]["path"].split("/")))  # restore parents first

        # 2a. Remove created paths
        for r in path_creations:
            try:
                await graph.remove_path(r["after"]["path"], r["after"]["domain"], session=session, namespace=r["after"].get("namespace"))
                messages.append(f"Removed path '{r['after']['path']}'.")
            except ValueError as e:
                if "not found" not in str(e):
                    raise e

        # 2b. Restore deleted paths
        for r in path_deletions:
            edge_id = r["before"].get("edge_id")
            edge_before = None

            # Try to find the original edge metadata from the changeset
            for er in rows:
                if er["table"] == "edges" and (er["before"] or {}).get("id") == edge_id:
                    edge_before = er["before"]
                    break

            parent_uuid = edge_before.get("parent_uuid") if edge_before else None
            child_uuid = edge_before.get("child_uuid") if edge_before else None
            priority = edge_before.get("priority", 0) if edge_before else 0
            disclosure = edge_before.get("disclosure") if edge_before else None

            # If edge wasn't deleted/changed, pull missing metadata from live DB
            if (parent_uuid is None or child_uuid is None) and edge_id:
                edge_db = (await session.execute(select(Edge).where(Edge.id == edge_id))).scalar_one_or_none()
                if edge_db:
                    parent_uuid = edge_db.parent_uuid
                    child_uuid = edge_db.child_uuid
                    priority = edge_db.priority
                    disclosure = edge_db.disclosure

            target_node_uuid = child_uuid or fallback_node_uuid or (r["before"].get("node_uuid") if r["before"] else None)

            await graph.restore_path(
                path=r["before"]["path"],
                domain=r["before"]["domain"],
                node_uuid=target_node_uuid,
                parent_uuid=parent_uuid,
                priority=priority,
                disclosure=disclosure,
                session=session,
                namespace=r["before"].get("namespace")
            )
            messages.append(f"Restored path '{r['before']['path']}'.")

        # 3. Revert Edge Metadata Changes (Priority / Disclosure)
        for r in rows:
            if r["table"] == "edges" and r["before"] and r["after"]:
                old_p = r["before"].get("priority")
                old_d = r["before"].get("disclosure")
                # If properties changed, issue an explicit DB UPDATE to revert them
                if old_p != r["after"].get("priority") or old_d != r["after"].get("disclosure"):
                    await session.execute(
                        update(Edge).where(Edge.id == r["before"]["id"]).values(
                            priority=old_p, disclosure=old_d
                        )
                    )
                    messages.append("Restored edge metadata.")

        # 4. Revert Memory Content (Revive old deprecated memory version)
        for r in rows:
            # Find the old memory row that was active ('deprecated' == False) before the changes
            if r["table"] == "memories" and r["before"] and not r["before"].get("deprecated"):
                old_active_mem_id = r["before"].get("id")
                try:
                    # rollback_to_memory automatically deprecates the current memory
                    # and un-deprecates the old one
                    # When admin does a rollback, it rebuilds FTS docs for ALL namespaces
                    await graph.rollback_to_memory(old_active_mem_id, session=session)
                    messages.append(f"Restored previous memory content ({old_active_mem_id}).")
                except ValueError as exc:
                    raise RuntimeError(
                        t("api.review.cannot_restore_memory").format(memory_id=old_active_mem_id, error=exc)
                    ) from exc

        # 5. Revert Glossary Keywords
        for r in rows:
            if r["table"] == "glossary_keywords":
                if r["before"] is None and r["after"] is not None:
                    # Revert creation -> delete
                    await session.execute(
                        delete(GlossaryKeyword).where(
                            GlossaryKeyword.keyword == r["after"]["keyword"],
                            GlossaryKeyword.node_uuid == r["after"]["node_uuid"],
                            GlossaryKeyword.namespace == r["after"].get("namespace", "")
                        )
                    )
                    messages.append(f"Reverted glossary keyword addition ('{r['after']['keyword']}').")
                elif r["before"] is not None and r["after"] is None:
                    # Revert deletion -> create
                    b = r["before"]

                    # Check if target node still exists to avoid foreign key error
                    node_exists = (await session.execute(
                        select(Node).where(Node.uuid == b["node_uuid"])
                    )).scalar_one_or_none()

                    if not node_exists:
                        messages.append(f"Target node for glossary keyword ('{b['keyword']}') no longer exists, skipped restore.")
                        continue

                    # Check if the keyword already exists (e.g. manually re-added) to avoid unique constraint conflict
                    existing = (await session.execute(
                        select(GlossaryKeyword).where(
                            GlossaryKeyword.keyword == b["keyword"],
                            GlossaryKeyword.node_uuid == b["node_uuid"],
                            GlossaryKeyword.namespace == b.get("namespace", "")
                        )
                    )).scalar_one_or_none()

                    if not existing:
                        entry = GlossaryKeyword(
                            keyword=b["keyword"],
                            node_uuid=b["node_uuid"],
                            namespace=b.get("namespace", "")
                        )
                        session.add(entry)
                        messages.append(f"Restored glossary keyword ('{b['keyword']}').")
                    else:
                        messages.append(f"Glossary keyword ('{b['keyword']}') already exists, skipped restore.")

    await search.rebuild_all_search_documents(session=session)
    if not messages:
        messages.append("No rollback action required.")
    return messages


async def _commit_pending_as_revision(session, store, author="ai", message=None):
    """Commit the full pending + approved pool as one revision node, update HEAD.

    If the pool is empty, no revision is created and HEAD is unchanged.
    Returns the new revision id, or None if nothing was committed.
    """
    rows = store.load_all_changed_rows()
    if not rows:
        return None
    parent_id = store.get_head_revision_id()
    new_id = await commit_revision(session, parent_id, "", rows, author=author, message=message)
    store.set_head_revision_id(new_id)
    store.drain_approved()
    store.reset_pool()
    return new_id


@router.post("/groups/{node_uuid}/rollback", response_model=GroupRollbackResponse)
async def rollback_group(node_uuid: str):
    """
    Execute universal rollback for all changes under a specific node_uuid.

    Revision-tree aware:
      1. Snapshot the current pending pool as an `author="ai"` revision (pre-rollback).
      2. Revert DB rows to their `before` state (universal rollback).
      3. Commit an `author="admin"` revision (the rollback branch), parent = pre-rollback.
      4. Update HEAD and clear the pool.
    """
    ctx = await _build_review_context()
    db = get_db_manager()
    graph = get_graph_service()
    search = get_search_indexer()

    rows = ctx.rows_for_node(node_uuid)
    if not rows:
        raise HTTPException(404, t("api.review.no_changes_for_node").format(node_uuid=node_uuid))

    try:
        store = ctx.store
        # Snapshot the pending pool so we can restore it if the revert fails.
        # (Original behavior: a failed rollback leaves the group in the pool.)
        pool_backup = store.load_all_changed_rows()
        old_head = store.get_head_revision_id()
        pre_rev_id = None

        # 1. Commit the current pool as a pre-rollback revision in its own session,
        #    so it persists independently of the revert outcome.
        if pool_backup:
            async with db.session() as session:
                pre_rev_id = await commit_revision(
                    session, old_head, "", pool_backup,
                    author="ai", message="pre-rollback snapshot",
                )
            store.set_head_revision_id(pre_rev_id)
            store.reset_pool()

        async with db.session() as session:
            # 2. Revert DB rows to before state (shared universal rollback logic).
            messages = await _revert_rows_to_before(session, graph, search, rows, fallback_node_uuid=node_uuid)

            # 3. Commit the rollback itself as an admin revision branching from HEAD.
            rollback_rows = {
                _make_row_key(r["table"], r["before"] if r["before"] else r["after"]): r
                for r in rows
            }
            await commit_revision(
                session, store.get_head_revision_id(), "", rollback_rows,
                author="admin", message=f"rollback node {node_uuid}",
            )
            from db.models import Revision
            from sqlalchemy import select
            new_rev = (await session.execute(
                select(Revision).order_by(Revision.id.desc()).limit(1)
            )).scalar_one()
            store.set_head_revision_id(new_rev.id)

        # 4. Pool already cleared in step 1 (or was empty).
        store.reset_pool()

        return GroupRollbackResponse(node_uuid=node_uuid, success=True, message=" ".join(messages))
    except Exception as e:
        # Restore the pool so the failed-rollback group is still reviewable.
        if pool_backup:
            ctx.store._restore_pool(pool_backup)
        # Rewind HEAD to its pre-rollback position (the pre-snapshot revision
        # stays in the tree as an abandoned node, but HEAD no longer points at it).
        ctx.store.set_head_revision_id(old_head)
        return GroupRollbackResponse(node_uuid=node_uuid, success=False, message=t("api.review.rollback_failed").format(error=e))


@router.delete("/groups/{node_uuid}")
async def approve_group(node_uuid: str):
    """
    Approve changes for a node group.

    Rows move into the `approved` staging pool (not deleted). When the pending
    pool drains to empty, the accumulated approved batch is committed as a
    single `author="ai"` revision on the tree and HEAD advances.
    """
    ctx = await _build_review_context()
    keys = ctx.keys_for_node(node_uuid)
    count = ctx.store.pool_approved(keys)
    if count == 0:
        raise HTTPException(404, t("api.review.no_changes_for_node").format(node_uuid=node_uuid))

    # If the pending pool is now empty, commit the approved batch as a revision.
    if ctx.store.get_change_count() == 0:
        db = get_db_manager()
        async with db.session() as session:
            await _commit_pending_as_revision(session, ctx.store, author="ai", message="approved batch")

    return {"message": t("api.review.approved_node").format(node_uuid=node_uuid, count=count)}


@router.delete("")
async def clear_all():
    """Approve/Integrate all pending changes globally by emptying the changeset.

    Commits the full pending pool as a single `author="ai"` revision, then
    clears the pool and advances HEAD.
    """
    store = get_changeset_store()
    count = store.get_change_count()
    if count == 0:
        raise HTTPException(404, t("api.review.no_pending_changes"))

    db = get_db_manager()
    async with db.session() as session:
        await _commit_pending_as_revision(session, store, author="ai", message="clear_all integration")
    # _commit_pending_as_revision already drains the approved pool and resets.
    return {"message": t("api.review.all_integrated").format(count=count)}


@router.get("/deprecated")
async def list_deprecated_memories():
    """List memories that have been replaced by newer versions."""
    graph = get_graph_service()
    memories = await graph.get_deprecated_memories()
    return {"count": len(memories), "memories": memories}



@router.post("/deprecated/{memory_id}/rollback")
async def rollback_deprecated_memory(memory_id: int, namespace: str = ""):
    """
    Roll back a deprecated memory to active status.
    
    Restores the specified deprecated memory as the active version,
    deprecating whatever is currently active for the same node.
    """
    from db import get_namespace
    graph = get_graph_service()
    ns = namespace or get_namespace()
    try:
        result = await graph.rollback_to_memory(memory_id, namespace=ns)
        return {"success": True, **result}
    except ValueError as e:
        raise HTTPException(404, str(e))
@router.delete("/memories/{memory_id}")
async def permanently_delete_memory(memory_id: int):
    """Permanently purge a deprecated memory from the DB (manual GC)."""
    graph = get_graph_service()
    try:
        await graph.permanently_delete_memory(memory_id)
        return {"message": t("api.review.memory_deleted").format(memory_id=memory_id)}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/diff", response_model=DiffResponse)
async def compare_text(request: DiffRequest):
    """Generic text diffing utility for the frontend."""
    diff_html, diff_unified, summary = get_text_diff(request.text_a, request.text_b)
    return DiffResponse(diff_html=diff_html, diff_unified=diff_unified, summary=summary)



@router.get("/revisions")
async def list_revisions(namespace: str = ""):
    """Return the revision tree (lightweight nodes), optionally namespace-filtered."""
    db = get_db_manager()
    async with db.session() as session:
        nodes = await get_revision_tree(session, namespace=namespace)
    return {"revisions": nodes, "head_revision_id": get_changeset_store().get_head_revision_id()}


@router.post("/revisions/{revision_id}/checkout")
async def checkout_revision_endpoint(revision_id: int):
    """Switch HEAD to `revision_id` by reverting to its state.

    First version supports only ancestor checkout (rewind to a prior point on
    the current branch). Cross-branch switching returns HTTP 501.

    Rewind strategy: walk HEAD -> target (exclusive), accumulating each
    revision's `before` snapshots; revert the accumulated rows to their
    `before` state; commit the checkout as an `author="admin"` revision whose
    parent is the target; advance HEAD to the new revision.
    """
    import json as _json
    from sqlalchemy import select
    from db.models import Revision

    store = get_changeset_store()
    db = get_db_manager()
    graph = get_graph_service()
    search = get_search_indexer()

    head_id = store.get_head_revision_id()

    async with db.session() as session:
        # 1. Commit any pending pool as a pre-checkout snapshot (if non-empty).
        await _commit_pending_as_revision(session, store, author="ai", message="pre-checkout snapshot")
        head_id = store.get_head_revision_id() or head_id

        target = await get_revision(session, revision_id)
        if target is None:
            raise HTTPException(404, f"revision {revision_id} not found")

        # 2. Ancestor check. Cross-branch -> 501.
        if head_id is not None and not await is_ancestor(session, revision_id, head_id):
            raise HTTPException(
                status_code=501,
                detail="跨分支切换暂未实现 (cross-branch checkout not implemented). "
                       "Only rewinding to an ancestor of the current HEAD is supported.",
            )

        # 3. Walk HEAD -> target (exclusive), accumulating `before` snapshots.
        accumulated: Dict[str, dict] = {}
        cur_id = head_id
        while cur_id is not None and cur_id != revision_id:
            cur = await get_revision(session, cur_id)
            if cur is None:
                break
            try:
                rev_rows = _json.loads(cur.changeset) if cur.changeset else {}
            except (ValueError, TypeError):
                rev_rows = {}
            for key, row in rev_rows.items():
                # Prefer the earliest `before` along the path (closest to target).
                if key not in accumulated and row.get("before") is not None:
                    accumulated[key] = row
            cur_id = cur.parent_id

        # 4. Revert accumulated rows to their before state.
        revert_rows = list(accumulated.values())
        if revert_rows:
            await _revert_rows_to_before(session, graph, search, revert_rows, fallback_node_uuid=None)

        # 5. Commit the checkout as an admin revision, parent = target.
        checkout_rows = {
            _make_row_key(r["table"], r["before"] if r["before"] else r["after"]): r
            for r in revert_rows
        }
        await commit_revision(
            session, revision_id, "", checkout_rows,
            author="admin", message=f"checkout to revision {revision_id}",
        )
        new_rev = (await session.execute(
            select(Revision).order_by(Revision.id.desc()).limit(1)
        )).scalar_one()
        store.set_head_revision_id(new_rev.id)

    store.reset_pool()
    return {
        "success": True,
        "head_revision_id": store.get_head_revision_id(),
        "checked_out_from": revision_id,
        "reverted_rows": len(accumulated),
    }