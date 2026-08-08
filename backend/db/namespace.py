"""
Namespace context for multi-agent memory isolation.

Uses contextvars to pass the namespace implicitly through async call chains.
For stdio mode, falls back to the NAMESPACE environment variable.
For SSE/HTTP mode, the middleware sets it per-request from the X-Namespace header.
"""

import contextvars
import os
from contextlib import asynccontextmanager


_namespace: contextvars.ContextVar[str] = contextvars.ContextVar(
    "namespace", default=os.getenv("NAMESPACE", "")
)


def get_namespace() -> str:
    return _namespace.get()


@asynccontextmanager
async def namespace_scope(ns: str):
    """Temporarily set the namespace for the duration of an async call.

    When character_id is passed to an MCP tool, wrap the tool body in
    ``async with namespace_scope(character_id):`` so that all downstream
    ``get_namespace()`` calls (system_views, graph service, etc.) resolve
    to the requested namespace without touching every call site.
    """
    token = _namespace.set(ns)
    try:
        yield
    finally:
        _namespace.reset(token)


def set_namespace(ns: str) -> contextvars.Token[str]:
    return _namespace.set(ns)
