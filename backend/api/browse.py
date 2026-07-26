"""
Browse API - Clean URI-based memory navigation

This replaces the old Entity/Relation/Chapter conceptual split with a simple
hierarchical browser. Every path is just a node with content and children.
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
import config
from db import get_graph_service, get_glossary_service, get_db_manager, get_search_indexer, get_preset_service
from db.models import Path as PathModel, Edge as EdgeModel, ROOT_NODE_UUID
from db.namespace import get_namespace
from locales import t
from sqlalchemy import select, distinct
import re

router = APIRouter(prefix="/browse", tags=["browse"])


class NodeUpdate(BaseModel):
    content: str | None = None
    priority: int | None = None
    disclosure: str | None = None


class GlossaryAdd(BaseModel):
    keyword: str
    node_uuid: str


class GlossaryRemove(BaseModel):
    keyword: str
    node_uuid: str


class CreateMemoryRequest(BaseModel):
    parent_path: str
    content: str
    priority: int
    disclosure: str
    title: str | None = None
    domain: str = "core"


class CreateAliasRequest(BaseModel):
    new_path: str
    target_path: str
    disclosure: str
    new_domain: str = "core"
    target_domain: str = "core"
    priority: int = 0


@router.get("/namespaces")
async def list_namespaces():
    """Return all distinct namespaces that exist in the paths table.

    Used by the Admin Dashboard namespace selector so the user can switch
    between agent memory spaces without knowing the exact strings upfront.
    An empty-string namespace is returned as "" and corresponds to the
    default (single-agent) namespace.
    """
    db = get_db_manager()
    async with db.session() as session:
        result = await session.execute(
            select(distinct(PathModel.namespace)).order_by(PathModel.namespace)
        )
        return [row[0] for row in result.all()]


@router.get("/domains")
async def list_domains():
    """Return all valid domains, with root-level node counts where applicable."""
    from sqlalchemy import func, distinct

    valid_domains = config.get("valid_domains") or ["core"]

    db = get_db_manager()
    async with db.session() as session:
        result = await session.execute(
            select(
                PathModel.domain,
                func.count(distinct(PathModel.path)).label("node_count"),
            )
            .where(PathModel.namespace == get_namespace())
            .where(~PathModel.path.contains("/"))
            .group_by(PathModel.domain)
            .order_by(PathModel.domain)
        )
        db_counts = {row.domain: row.node_count for row in result.all()}

    domains_to_return = []
    seen = set()
    for d in valid_domains:
        domains_to_return.append({"domain": d, "root_count": db_counts.get(d, 0)})
        seen.add(d)

    for d, count in db_counts.items():
        if d not in seen:
            domains_to_return.append({"domain": d, "root_count": count})
            seen.add(d)

    return domains_to_return


class AddDomainRequest(BaseModel):
    domain: str


@router.post("/domains")
async def add_domain(body: AddDomainRequest):
    domain = body.domain.strip().lower()
    if not re.match(r'^[a-z][a-z0-9_]*$', domain):
        raise HTTPException(status_code=422, detail=t("api.settings.validation_error"))
    if domain == "system":
        raise HTTPException(status_code=400, detail=t("api.settings.system_reserved_error"))

    current = list(config.get("valid_domains") or ["core"])
    if domain not in current:
        current.append(domain)
        config.set_value("valid_domains", current)
        return {"success": True, "domain": domain, "added": True}

    return {"success": True, "domain": domain, "added": False}


@router.delete("/domains/{domain}")
async def remove_domain(domain: str):
    domain = domain.strip().lower()
    if domain in ("core", "system"):
        raise HTTPException(status_code=400, detail=t("api.settings.core_remove_error"))
        
    db = get_db_manager()
    async with db.session() as session:
        result = await session.execute(
            select(PathModel.path)
            .where(PathModel.domain == domain)
            .limit(1)
        )
        if result.first():
            raise HTTPException(
                status_code=409, 
                detail=t("api.settings.domain_in_use_error")
            )

    # Check if any boot URI across ALL presets references this domain
    service = get_preset_service()
    all_presets = await service.list_presets()
    domain_prefix = f"{domain}://"
    referencing = []
    for preset in all_presets:
        for ns, uris in preset["boot_uris"].items():
            if any(u == domain_prefix or u.startswith(domain_prefix) for u in uris):
                label = ns if ns else "(default)"
                referencing.append(f"{preset['name']}:{label}")

    if referencing:
        raise HTTPException(
            status_code=409,
            detail=t("api.settings.domain_boot_uri_conflict").format(
                domain=domain, namespaces=", ".join(referencing)
            ),
        )

    current = list(config.get("valid_domains") or ["core"])
    if domain in current:
        current.remove(domain)
        config.set_value("valid_domains", current)

    return {"success": True, "domain": domain}


@router.get("/node")
async def get_node(
    path: str = Query("", description="URI path like 'nocturne' or 'nocturne/salem'"),
    domain: str = Query("core"),
    nav_only: bool = Query(False, description="Skip expensive processing if only navigating tree")
):
    """
    Get a node's content and its direct children.
    
    This is the only read endpoint you need - it gives you:
    - The current node's full content (or virtual root)
    - Preview of all children (next level)
    - Breadcrumb trail for navigation
    """
    graph = get_graph_service()
    
    if not path:
        # Check if there is an actual memory stored at the root path
        memory = await graph.get_memory_by_path("", domain=domain, namespace=get_namespace())
        
        children_raw = await graph.get_children(
            ROOT_NODE_UUID,
            context_domain=domain,
            context_path=path,
            namespace=get_namespace()
        )
        
        if memory:
            # Hide the actual root node from the root directory listing.
            children_raw = [
                c for c in children_raw
                if c.get("node_uuid") != memory["node_uuid"]
            ]
        else:
            # Virtual Root Node
            memory = {
                "content": "",
                "priority": 0,
                "disclosure": None,
                "created_at": None,
                "node_uuid": ROOT_NODE_UUID,
            }
            
        breadcrumbs = [{"path": "", "label": "root"}]
    else:
        # Get the node itself
        memory = await graph.get_memory_by_path(path, domain=domain, namespace=get_namespace())
        
        if not memory:
            raise HTTPException(status_code=404, detail=t("api.browse.path_not_found").format(uri=f"{domain}://{path}"))
        
        children_raw = await graph.get_children(
            memory["node_uuid"],
            context_domain=domain,
            context_path=path,
            namespace=get_namespace()
        )
        
        # Build breadcrumbs
        segments = path.split("/")
        breadcrumbs = [{"path": "", "label": "root"}]
        accumulated = ""
        for seg in segments:
            accumulated = f"{accumulated}/{seg}" if accumulated else seg
            breadcrumbs.append({"path": accumulated, "label": seg})
    
    children = [
        {
            "domain": c["domain"],
            "path": c["path"],
            "uri": f"{c['domain']}://{c['path']}",
            "name": c["path"].split("/")[-1],  # Last segment
            "priority": c["priority"],
            "disclosure": c.get("disclosure"),
            "content_snippet": c["content_snippet"],
            "approx_children_count": c.get("approx_children_count", 0)
        }
        for c in children_raw
        if c["domain"] == domain and c["path"] != ""
    ]
    children.sort(key=lambda x: (x["priority"] if x["priority"] is not None else 999, x["path"]))
    
    # Get all aliases (other paths pointing to the same node)
    aliases = []
    if memory.get("node_uuid") and memory["node_uuid"] != ROOT_NODE_UUID:
        async with get_db_manager().session() as session:
            result = await session.execute(
                select(PathModel.domain, PathModel.path)
                .select_from(PathModel)
                .join(EdgeModel, PathModel.edge_id == EdgeModel.id)
                .where(PathModel.namespace == get_namespace())
                .where(EdgeModel.child_uuid == memory["node_uuid"])
            )
            aliases = [
                f"{row[0]}://{row[1]}"
                for row in result.all()
                if not (row[0] == domain and row[1] == path)
            ]
    
    # Get glossary keywords for this node
    glossary_keywords = []
    glossary_matches = []
    node_uuid = memory.get("node_uuid")

    if not nav_only:
        _glossary = get_glossary_service()
        if node_uuid and node_uuid != ROOT_NODE_UUID:
            glossary_keywords = await _glossary.get_glossary_for_node(node_uuid, namespace=get_namespace())

        # Get all glossary matches for the node content using Aho-Corasick
        if memory.get("content"):
            matches_dict = await _glossary.find_glossary_in_content(memory["content"], namespace=get_namespace())
            if matches_dict:
                glossary_matches = [
                    {"keyword": kw, "nodes": nodes}
                    for kw, nodes in matches_dict.items()
                ]

    return {
        "node": {
            "path": path,
            "domain": domain,
            "uri": f"{domain}://{path}",
            "name": path.split("/")[-1] if path else "root",
            "content": memory["content"],
            "priority": memory["priority"],
            "disclosure": memory["disclosure"],
            "created_at": memory["created_at"],
            "is_virtual": memory.get("created_at") is None,
            "aliases": aliases,
            "node_uuid": node_uuid,
            "glossary_keywords": glossary_keywords,
            "glossary_matches": glossary_matches,
        },
        "children": children,
        "breadcrumbs": breadcrumbs
    }


@router.put("/node")
async def update_node(
    path: str = Query(...),
    domain: str = Query("core"),
    body: NodeUpdate = ...
):
    """
    Update a node's content.
    """
    graph = get_graph_service()
    
    # Check exists
    memory = await graph.get_memory_by_path(path, domain=domain, namespace=get_namespace())
    if not memory:
        raise HTTPException(status_code=404, detail=t("api.browse.path_not_found").format(uri=f"{domain}://{path}"))
    
    # Update (creates new version if content changed, updates path metadata otherwise)
    try:
        result = await graph.update_memory(
            path=path,
            domain=domain,
            content=body.content,
            priority=body.priority,
            disclosure=body.disclosure,
            namespace=get_namespace(),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    
    return {"success": True, "memory_id": result["new_memory_id"]}


@router.post("/node")
async def create_node(body: CreateMemoryRequest):
    """
    Create a new memory node.

    Human-facing direct edit endpoint: intentionally bypasses changeset/review.
    """
    graph = get_graph_service()

    if not body.disclosure:
        raise HTTPException(status_code=422, detail=t("api.browse.disclosure_empty"))

    if body.title is not None and not re.match(r'^[a-zA-Z0-9_-]+$', body.title):
        raise HTTPException(status_code=422, detail=t("api.browse.invalid_title"))

    try:
        result = await graph.create_memory(
            parent_path=body.parent_path,
            content=body.content,
            priority=body.priority,
            title=body.title,
            disclosure=body.disclosure,
            domain=body.domain,
            namespace=get_namespace(),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return {"success": True, "uri": result["uri"], "memory_id": result["id"]}


@router.post("/node/alias")
async def create_alias(body: CreateAliasRequest):
    """
    Add an alias (alternate path) to an existing node.

    Human-facing direct edit endpoint: intentionally bypasses changeset/review.
    """
    graph = get_graph_service()

    if not body.disclosure:
        raise HTTPException(status_code=422, detail=t("api.browse.disclosure_empty"))

    try:
        await graph.add_path(
            new_path=body.new_path,
            target_path=body.target_path,
            new_domain=body.new_domain,
            target_domain=body.target_domain,
            priority=body.priority,
            disclosure=body.disclosure,
            namespace=get_namespace(),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return {"success": True, "uri": f"{body.new_domain}://{body.new_path}"}


class RenameRequest(BaseModel):
    path: str
    new_name: str
    domain: str = "core"


@router.post("/node/rename")
async def rename_node(body: RenameRequest):
    """
    Rename a memory by changing the last segment of its URI path.

    Like renaming a folder: the node and all its children are moved
    to the new path via add_path cascade + remove_path cleanup.

    Human-facing direct edit endpoint: intentionally bypasses changeset/review.
    """
    graph = get_graph_service()

    if not re.match(r'^[a-zA-Z0-9_-]+$', body.new_name):
        raise HTTPException(
            status_code=422,
            detail=t("api.browse.invalid_name"),
        )

    old_path = body.path
    old_uri = f"{body.domain}://{old_path}"

    if "/" in old_path:
        parent = old_path.rsplit("/", 1)[0]
        new_path = f"{parent}/{body.new_name}"
    else:
        new_path = body.new_name
    new_uri = f"{body.domain}://{new_path}"

    if old_path == new_path:
        return {"success": True, "old_uri": old_uri, "new_uri": new_uri, "unchanged": True}

    memory = await graph.get_memory_by_path(old_path, domain=body.domain, namespace=get_namespace())
    if not memory:
        raise HTTPException(status_code=404, detail=t("api.browse.path_not_found").format(uri=old_uri))

    try:
        await graph.add_path(
            new_path=new_path,
            target_path=old_path,
            new_domain=body.domain,
            target_domain=body.domain,
            priority=memory.get("priority", 0),
            disclosure=memory.get("disclosure"),
            namespace=get_namespace(),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Remove old path. If this fails, roll back the new path to avoid
    # leaving the node in a silent dual-path state the user won't notice.
    try:
        await graph.remove_path(old_path, body.domain, namespace=get_namespace())
    except Exception as e:
        try:
            await graph.remove_path(new_path, body.domain, namespace=get_namespace())
        except Exception:
            pass  # best-effort rollback
        raise HTTPException(
            status_code=500,
            detail=t("api.browse.rename_partial_failure").format(error=e),
        )

    await get_preset_service().rewrite_boot_uri(old_uri, new_uri, get_namespace())

    return {
        "success": True,
        "old_uri": old_uri,
        "new_uri": new_uri,
        "old_path": old_path,
        "new_path": new_path,
    }


# =============================================================================
# Glossary Endpoints
# =============================================================================


@router.get("/glossary")
async def get_glossary():
    """Get all glossary keywords with their associated nodes."""
    glossary = get_glossary_service()
    raw_entries = await glossary.get_all_glossary(namespace=get_namespace(), search_all_namespaces=False)
    
    return {"glossary": raw_entries}


@router.post("/glossary")
async def add_glossary_keyword(body: GlossaryAdd):
    """Bind a keyword to a node."""
    # Human-facing direct edit endpoint: intentionally bypasses changeset/review.
    # The review queue tracks AI-authored mutations only.
    glossary = get_glossary_service()
    try:
        result = await glossary.add_glossary_keyword(body.keyword, body.node_uuid, namespace=get_namespace())
        return {"success": True, **result}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.delete("/glossary")
async def remove_glossary_keyword(body: GlossaryRemove):
    """Remove a keyword binding from a node."""
    # Human-facing direct edit endpoint: intentionally bypasses changeset/review.
    # The review queue tracks AI-authored mutations only.
    glossary = get_glossary_service()
    result = await glossary.remove_glossary_keyword(body.keyword, body.node_uuid, namespace=get_namespace())
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=t("api.browse.keyword_not_found"))
    return {"success": True}


# =============================================================================
# Delete Endpoint
# =============================================================================


@router.delete("/node")
async def delete_node(
    path: str = Query(...),
    domain: str = Query("core"),
):
    """
    Delete a memory by removing its URI path.

    Human-facing direct delete: bypasses changeset/review queue.
    Calls graph.remove_path() which handles orphan prevention.
    """
    graph = get_graph_service()

    memory = await graph.get_memory_by_path(path, domain=domain, namespace=get_namespace())
    if not memory:
        raise HTTPException(status_code=404, detail=t("api.browse.path_not_found").format(uri=f"{domain}://{path}"))

    try:
        await graph.remove_path(path, domain, namespace=get_namespace())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    deleted_uri = f"{domain}://{path}"
    await get_preset_service().purge_boot_uri(deleted_uri, get_namespace())

    return {"success": True, "uri": deleted_uri}


# =============================================================================
# Search Endpoint
# =============================================================================


@router.get("/search")
async def search_memories(
    q: str = Query(..., min_length=1, description="Search query"),
    domain: Optional[str] = Query(None, description="Optional domain filter"),
    limit: int = Query(20, ge=1, le=100),
):
    """Full-text search across memories using the FTS index."""
    search = get_search_indexer()
    results = await search.search(q, limit=limit, domain=domain, namespace=get_namespace())
    return {"query": q, "results": results, "count": len(results)}


# =============================================================================
# Recent Memories Endpoint
# =============================================================================


@router.get("/recent")
async def recent_memories(
    limit: int = Query(10, ge=1, le=50, description="Number of recent memories to return"),
    domain: Optional[str] = Query(None, description="Optional domain filter, e.g. 'core'"),
):
    """Return the most recently created memories."""
    graph = get_graph_service()
    raw = await graph.get_recent_memories(limit=limit, namespace=get_namespace(), domain=domain)
    
    # 补充 content_snippet
    results = []
    for mem in raw:
        snippet = ""
        # 尝试从数据库获取 content
        try:
            from db import get_db_manager
            from db.models import Memory as MemoryModel
            from sqlalchemy import select
            db = get_db_manager()
            async with db.session() as session:
                row = await session.execute(
                    select(MemoryModel.content).where(MemoryModel.id == mem["memory_id"]).limit(1)
                )
                content = row.scalar_one_or_none()
                if content:
                    snippet = content[:80] + ("..." if len(content) > 80 else "")
        except Exception:
            pass
        
        results.append({
            **mem,
            "content_snippet": snippet,
        })
    
    return {"memories": results, "count": len(results)}
