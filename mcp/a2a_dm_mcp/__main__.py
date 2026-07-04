"""Entrypoint for ``a2a-dm-mcp`` console script.

MCP servers are spawned by the client (Claude Desktop, Cursor,
Cline, Continue, etc.) as a subprocess speaking JSON-RPC over
stdio. The client manages the lifecycle — we just have to start
the server and block until stdin closes.

Run directly: ``python -m a2a_dm_mcp`` or via the installed
entrypoint ``a2a-dm-mcp``.
"""

from __future__ import annotations

import sys

from a2a_dm_mcp.server import build_server


def main() -> int:
    """Boot the server on stdio. Returns the exit code."""
    try:
        server = build_server()
        # FastMCP.run() handles the stdio transport setup + JSON-RPC
        # message loop. Blocks until stdin closes.
        server.run("stdio")
        return 0
    except KeyboardInterrupt:
        # Cleanly handle Ctrl-C — common when the user is debugging
        # locally outside an MCP client.
        return 0
    except Exception as e:
        # Stderr only — stdout is reserved for JSON-RPC frames.
        print(f"a2a-dm-mcp: fatal error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
