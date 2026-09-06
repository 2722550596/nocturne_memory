"""Pydantic models for structured MCP tool return values.

Each writing memory tool returns one of these models instead of a bare string.
FastMCP derives an `outputSchema` from the type annotation, so the dict lands in
the MCP `structuredContent` field alongside the human-readable `message` text.

The `message` field is what the AI reads. The remaining fields are consumed by
the pi-rp session-rollback extension (via pi-mcp-adapter's structuredContent
passthrough) to anchor DB state to the conversation tree.
"""

from typing import List, Optional
from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    revision_id: Optional[int] = Field(default=None, description="快照节点 ID（回滚用）")
    message: str = Field(..., description="给 AI 看的中文结果消息")


class NodeResult(ToolResult):
    """Result that touches a single node_uuid."""
    node_uuid: str = Field(..., description="被操作的节点 UUID")
    uri: str = Field(..., description="主入口 URI")


class CreateResult(NodeResult):
    """remember_child_memory / merge / organize create a fresh node."""
    pass


class UpdateResult(NodeResult):
    """edit_memory modifies an existing node's content/metadata."""
    old_memory_id: Optional[int] = None
    new_memory_id: Optional[int] = None


class ForgetResult(ToolResult):
    """forget_memory may cascade-delete several nodes."""
    deleted_node_uuids: List[str] = Field(default_factory=list)


class LinkResult(ToolResult):
    """link_memory adds an alias path to an existing node."""
    target_uri: str = Field(..., description="被指向的目标 URI")
    new_uri: str = Field(..., description="新入口 URI")


class TagResult(ToolResult):
    """tag_memory adds/removes glossary keywords on a node."""
    node_uuid: str = Field(..., description="被贴标签的节点 UUID")
    added: List[str] = Field(default_factory=list)
    removed: List[str] = Field(default_factory=list)


class ArchiveResult(ToolResult):
    """archive_history records a scene to the history domain."""
    uri: str = Field(..., description="存档后的 URI，如 history://scenes/title")
