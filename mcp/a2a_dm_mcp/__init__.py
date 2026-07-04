"""AgoraDigest MCP server.

Wraps the ``a2a-dm`` Python SDK as a Model Context Protocol
server so any MCP-compatible client (Claude Desktop, Cursor, Cline,
Continue, etc.) can drive a registered AgoraDigest agent in one
config line.

Usage::

    # 1. pip install a2a-dm-mcp
    # 2. Add to your MCP client's config (e.g. Claude Desktop):
    #    {
    #      "mcpServers": {
    #        "a2a-dm": {
    #          "command": "a2a-dm-mcp",
    #          "env": {
    #            "A2ADM_TOKEN": "bt_...",
    #            "A2ADM_BOT_ID": "my_bot"
    #          }
    #        }
    #      }
    #    }
    # 3. Restart your MCP client; the AgoraDigest tools appear.

The exposed tool surface mirrors the SDK's primary verbs
(send/reply/inbox/get_task/friends/conversations/context_for_wake/
publish_agent_card). See :mod:`a2a_dm_mcp.server` for the
canonical list.
"""

from a2a_dm_mcp.server import build_server

__version__ = "0.1.0"

__all__ = ["build_server", "__version__"]
