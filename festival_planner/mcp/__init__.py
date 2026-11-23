"""The embedded MCP endpoint for Claude and Duet (design §4.3). See `server.py`."""

from festival_planner.mcp.server import PathTokenMiddleware, check_args, register_mcp, tool

__all__ = ["PathTokenMiddleware", "check_args", "register_mcp", "tool"]
