# pyright: reportMissingImports=false

"""
HTTP RPC layer exposing Nocturne memory tools to pi extensions.

pi extensions (TypeScript, via pi.registerTool) call the ``/api/pi-tools/invoke``
endpoint with ``fetch()`` instead of spawning a per-session MCP server.  The
single long-lived web server (``python main.py``) owns all the business logic,
so pi sessions start with zero Python startup cost while keeping 100% of the
existing tool behaviour (system views, write safety, namespace isolation).

Each tool function in mcp_server.py is reused verbatim; only the transport
changes (MCP JSON-RPC -> HTTP POST).  Namespace is applied per request via
``namespace_scope`` so ``get_namespace()`` resolves correctly downstream.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

import config as _cfg
from db.namespace import namespace_scope

router = APIRouter(prefix="/pi-tools", tags=["pi-tools"])

# Lazy import of the underlying tool functions.  Importing mcp_server at module
# level would create a circular import (mcp_server imports web_app, which
# imports this module), so we defer it until the first request, by which time
# web_app is fully loaded.
_TOOL_FUNCS: Optional[Dict[str, Any]] = None


def _tools() -> Dict[str, Any]:
    global _TOOL_FUNCS
    if _TOOL_FUNCS is None:
        from mcp_server import (
            archive_history,
            batch_edit_memories,
            batch_forget_memories,
            batch_move_memories,
            boot_memory,
            browse_memory,
            edit_memory,
            forget_memory,
            link_memory,
            merge_memories,
            move_memory,
            organize_memory,
            recent_memories,
            remember_child_memory,
            remember_memory,
            rename_memory,
            search_memory,
            set_world_time,
            tag_memory,
        )
        _TOOL_FUNCS = {
            "browse_memory": browse_memory,
            "search_memory": search_memory,
            "remember_memory": remember_memory,
            "set_world_time": set_world_time,
            "remember_child_memory": remember_child_memory,
            "edit_memory": edit_memory,
            "forget_memory": forget_memory,
            "link_memory": link_memory,
            "tag_memory": tag_memory,
            "merge_memories": merge_memories,
            "organize_memory": organize_memory,
            "rename_memory": rename_memory,
            "move_memory": move_memory,
            "batch_move_memories": batch_move_memories,
            "batch_forget_memories": batch_forget_memories,
            "batch_edit_memories": batch_edit_memories,
            "archive_history": archive_history,
            "recent_memories": recent_memories,
            "boot_memory": boot_memory,
        }
    return _TOOL_FUNCS


class InvokeRequest(BaseModel):
    name: str = Field(..., description="Tool name, e.g. browse_memory")
    params: Dict[str, Any] = Field(default_factory=dict, description="Tool arguments")
    namespace: Optional[str] = Field(
        default=None,
        description="Optional namespace (character_id equivalent). Falls back to X-Namespace header / default.",
    )
    world_clock: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional per-request world-clock override: {enabled: bool, current_time?: str}. "
        "enabled=True uses world-clock mode (current_time or process config); enabled=False uses real clock.",
    )


class InvokeResponse(BaseModel):
    ok: bool
    message: str
    data: Dict[str, Any] = Field(default_factory=dict)


def _serialize(result: Any) -> InvokeResponse:
    """Normalize a tool result (str or structured model) into InvokeResponse.

    ``message`` is what the LLM reads; ``data`` carries the structured fields
    (revision_id, node_uuid, uri, ...) for the pi-rp session-rollback extension.
    """
    if isinstance(result, str):
        return InvokeResponse(ok=True, message=result)
    if hasattr(result, "model_dump"):
        dump = result.model_dump()
        message = dump.pop("message", None) or str(result)
        return InvokeResponse(ok=True, message=message, data=dump)
    return InvokeResponse(ok=True, message=str(result))


@router.post("/invoke", response_model=InvokeResponse)
async def invoke(body: InvokeRequest) -> InvokeResponse:
    tools = _tools()
    fn = tools.get(body.name)
    if fn is None:
        return InvokeResponse(
            ok=False,
            message=f"未知工具：{body.name}。可用：{', '.join(sorted(tools))}",
        )

    async def _call() -> Any:
        # Scope a per-request world-clock override so one process can serve
        # multiple roles with different clocks. current_time omitted -> the
        # process config value is used (e.g. the magnolia 2020 world).
        if body.world_clock is not None:
            token = _cfg.set_world_clock_override(
                enabled=bool(body.world_clock.get("enabled", True)),
                current_time=body.world_clock.get("current_time"),
            )
            try:
                return await fn(**body.params)
            finally:
                _cfg.reset_world_clock_override(token)
        return await fn(**body.params)

    try:
        if body.namespace is not None:
            async with namespace_scope(body.namespace):
                result = await _call()
        else:
            result = await _call()
        return _serialize(result)
    except Exception as e:  # noqa: BLE001 - tools already catch most, be defensive
        return InvokeResponse(ok=False, message=f"调用出错：{e}")


class SlotRequest(BaseModel):
    slot_type: str = Field(..., description="Slot view type: boot | history | state")
    namespace: Optional[str] = Field(
        default=None,
        description="Optional namespace (character_id equivalent). Falls back to X-Namespace header / default.",
    )


class SlotResponse(BaseModel):
    ok: bool
    content: str


@router.post("/slot", response_model=SlotResponse)
async def render_slot(body: SlotRequest) -> SlotResponse:
    """Render a memory slot view in-process (no Python cold start).

    Mirrors query_slot.py: resolves boot URIs for the namespace, then generates
    the slot view. Runs inside the long-lived server, so a slot render costs
    milliseconds instead of a fresh venv startup per call.
    """
    from db import get_preset_service
    from system_views import generate_memory_slot_view

    async def _call() -> str:
        preset = get_preset_service()
        boot_uris = await preset.get_boot_uris(namespace=body.namespace or "")
        return await generate_memory_slot_view(body.slot_type, boot_uris)

    try:
        if body.namespace is not None:
            async with namespace_scope(body.namespace):
                content = await _call()
        else:
            content = await _call()
        return SlotResponse(ok=True, content=content)
    except Exception as e:  # noqa: BLE001 - defensive, mirrors invoke()
        return SlotResponse(ok=False, content=f"[Error rendering {body.slot_type} slot: {e}]")
