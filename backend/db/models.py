# pyright: reportArgumentType=false, reportAttributeAccessIssue=false

"""
ORM Models and shared utilities for Nocturne Memory System.

Graph-based memory storage with:
- Node: a conceptual entity (UUID), version-independent
- Memory: a content version of a node
- Edge: parent→child relationship between nodes, carrying metadata
- Path: materialized URI cache (domain://path → edge)
"""

from datetime import datetime
from typing import Dict, Any, List

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    Index,
    text,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

# Sentinel root node — parent_uuid of all top-level edges.
# Using a fixed UUID instead of NULL avoids SQLite's NULL != NULL uniqueness quirk.
ROOT_NODE_UUID = "00000000-0000-0000-0000-000000000000"


# =============================================================================
# Shared Utilities
# =============================================================================


def escape_like_literal(value: str) -> str:
    """Escape special chars in SQL LIKE patterns for literal matching."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def serialize_row(obj) -> Dict[str, Any]:
    """Convert an ORM model instance to a plain dict for snapshot storage."""
    d = {}
    for col in obj.__table__.columns:
        val = getattr(obj, col.name)
        if isinstance(val, datetime):
            val = val.isoformat()
        d[col.name] = val
    return d


def serialize_memory_ref(obj) -> Dict[str, Any]:
    """Serialize a Memory row as a pointer (no content).

    The actual content stays in the DB and is resolved at review time.
    """
    d = serialize_row(obj)
    d.pop("content", None)
    return d


# =============================================================================
# ORM Models
# =============================================================================


class Node(Base):
    """A conceptual entity whose UUID persists across content versions.

    Edges reference nodes by UUID, so updating a memory's content (which
    creates a new Memory row) never requires touching the graph structure.
    """

    __tablename__ = "nodes"

    uuid = Column(String(36), primary_key=True)
    created_at = Column(DateTime, default=datetime.now)
    last_accessed_at = Column(DateTime, nullable=True)

    memories = relationship("Memory", back_populates="node")
    child_edges = relationship(
        "Edge", foreign_keys="Edge.child_uuid", back_populates="child_node"
    )
    parent_edges = relationship(
        "Edge", foreign_keys="Edge.parent_uuid", back_populates="parent_node"
    )


class Memory(Base):
    """A single content version of a node.

    Version chain: old.migrated_to → new.id.  All versions of the same
    conceptual entity share the same node_uuid.
    """

    __tablename__ = "memories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_uuid = Column(String(36), ForeignKey("nodes.uuid"), nullable=True)
    content = Column(Text, nullable=False)
    deprecated = Column(Boolean, default=False)
    migrated_to = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    world_timestamp = Column(Text, nullable=True)

    node = relationship("Node", back_populates="memories")


class Edge(Base):
    """Directed parent→child relationship between two nodes.

    Carries display name, priority, and disclosure.  The (parent_uuid,
    child_uuid) pair is unique — one edge per structural relationship.
    Multiple Path rows can reference the same edge (aliases).
    """

    __tablename__ = "edges"

    id = Column(Integer, primary_key=True, autoincrement=True)
    parent_uuid = Column(String(36), ForeignKey("nodes.uuid"), nullable=False)
    child_uuid = Column(String(36), ForeignKey("nodes.uuid"), nullable=False)
    name = Column(String(256), nullable=False)
    priority = Column(Integer, default=0)
    disclosure = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("parent_uuid", "child_uuid", name="uq_edge_parent_child"),
    )

    parent_node = relationship(
        "Node", foreign_keys=[parent_uuid], back_populates="parent_edges"
    )
    child_node = relationship(
        "Node", foreign_keys=[child_uuid], back_populates="child_edges"
    )
    paths = relationship("Path", back_populates="edge")


class Path(Base):
    """Materialized URI cache: (namespace, domain, path_string) → edge.

    The source of truth for tree structure is the edges table.
    Paths are a routing convenience for URI resolution.
    Namespace enables multi-agent memory isolation within a single instance.
    """

    __tablename__ = "paths"

    namespace = Column(String(64), primary_key=True, default="")
    domain = Column(String(64), primary_key=True, default="core")
    path = Column(String(512), primary_key=True)
    edge_id = Column(Integer, ForeignKey("edges.id"), nullable=True)
    node_uuid = Column(
        String(36),
        ForeignKey("nodes.uuid"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime, default=datetime.now)

    edge = relationship("Edge", back_populates="paths")
    node = relationship("Node")


class GlossaryKeyword(Base):
    """Glossary keyword-to-node binding (豆辞典).

    When a keyword appears in a memory's content, the MCP layer surfaces
    the associated nodes and the frontend highlights the keyword.
    Multiple keywords can point to the same node, and the same keyword
    can point to multiple nodes.
    """

    __tablename__ = "glossary_keywords"

    id = Column(Integer, primary_key=True, autoincrement=True)
    keyword = Column(Text, nullable=False)
    node_uuid = Column(
        String(36),
        ForeignKey("nodes.uuid", ondelete="CASCADE"),
        nullable=False,
    )
    namespace = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("keyword", "node_uuid", "namespace", name="uq_glossary_keyword_node"),
    )

    node = relationship("Node")


class SearchDocument(Base):
    """Derived search row for one reachable path of an active node."""

    __tablename__ = "search_documents"

    namespace = Column(String(64), primary_key=True, default="")
    domain = Column(String(64), primary_key=True, default="core")
    path = Column(String(512), primary_key=True)
    node_uuid = Column(
        String(36),
        ForeignKey("nodes.uuid", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    memory_id = Column(
        Integer,
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
    )
    uri = Column(Text, nullable=False)
    content = Column(Text, nullable=False)
    disclosure = Column(Text, nullable=True)
    # Stores glossary keywords plus auxiliary CJK search terms.
    search_terms = Column(Text, nullable=False, default="")
    priority = Column(Integer, nullable=False, default=0)
    world_timestamp = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.now)


class MemoryAccessLog(Base):
    """Asynchronous access log for tracking memory reading frequency and sequences."""

    __tablename__ = "memory_access_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_uuid = Column(
        String(36),
        ForeignKey("nodes.uuid", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    namespace = Column(String(64), nullable=False, default="")
    accessed_at = Column(DateTime, default=datetime.now, index=True)
    context = Column(String(64), nullable=True)

    node = relationship("Node")


class Preset(Base):
    """Boot URI preset — a named set of boot URIs and path masks.

    Only one preset can be active at a time (is_active=True).
    The active preset determines which URIs are loaded at boot.
    """

    __tablename__ = "presets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False, unique=True)
    boot_uris = Column(Text, nullable=False, default="{}")
    path_masks = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index(
            "uq_presets_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active = true"),
            sqlite_where=text("is_active = 1"),
        ),
    )


# =============================================================================
# Revision Tree
# =============================================================================


class Revision(Base):
    """不可变快照节点。每次 AI 写操作生成一个，parent_id 构成版本树。

    与 ChangesetStore 的 changeset.json 同构：changeset 字段存储完整的行级
    before/after 状态。回滚/分支在树上长出新叶子而非破坏性翻转。
    """
    __tablename__ = "revisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    parent_id = Column(Integer, ForeignKey("revisions.id"), nullable=True)
    namespace = Column(String(64), nullable=False, default="")
    # 完整的行级 before/after 状态，与 ChangesetStore 的 changeset.json 同构:
    # {row_key: {"table":..., "before":..., "after":...}}
    changeset = Column(Text, nullable=False)  # JSON
    author = Column(String(32), nullable=False, default="ai")  # "ai" | "admin" | "system"
    message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    parent = relationship("Revision", remote_side=[id], backref="children")

# =============================================================================
# Change Collector
# =============================================================================


class ChangeCollector:
    """Accumulates serialized row data before mutations for changeset recording.

    Passed optionally through the operation layers so that each delete
    primitive can record pre-deletion state without coupling the "what to
    record" concern into the "what to delete" logic.

    Memory rows are stored as pointers only (no content) — the actual
    content lives in the DB (deprecated but not deleted) and can be
    resolved on the fly at review time.
    """

    def __init__(self):
        self.nodes: List[Dict[str, Any]] = []
        self.memories: List[Dict[str, Any]] = []
        self.edges: List[Dict[str, Any]] = []
        self.paths: List[Dict[str, Any]] = []
        self.glossary_keywords: List[Dict[str, Any]] = []

    def record(self, table: str, row_data: Dict[str, Any]):
        if table == "memories":
            row_data = {k: v for k, v in row_data.items() if k != "content"}
        getattr(self, table).append(row_data)

    def to_dict(self) -> Dict[str, list]:
        return {
            "nodes": self.nodes,
            "memories": self.memories,
            "edges": self.edges,
            "paths": self.paths,
            "glossary_keywords": self.glossary_keywords,
        }
