# mcp_server/__init__.py
"""Read-only MCP server exposing the parts database to external LLM clients."""

from .protocol import mcp_bp
from .tunnel import install_tunnel_guard, maybe_start_tunnel

__all__ = ["mcp_bp", "install_tunnel_guard", "maybe_start_tunnel"]
