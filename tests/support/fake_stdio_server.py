"""A tiny stdio MCP server standing in for one that acts as you, such as emacs-org-mcp."""

# system imports
from typing import Any

# 3rd party imports
from mcp import MCPError
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import INVALID_PARAMS

mcp = MCPServer("fake-org")


@mcp.tool()
def get_thing(key: str, verbose: bool = False) -> dict[str, Any]:
    """Return one thing by its key."""
    return {"key": key, "value": f"value-of-{key}", "verbose": verbose}


@mcp.tool()
def echo_meta(ctx: Context) -> dict[str, Any]:
    """Return the caller identity the request's _meta carried."""
    meta = ctx.request_context.meta or {}
    return {k: v for k, v in meta.items() if k.startswith("maji-mcp/")}


@mcp.tool()
def set_thing(key: str, value: str) -> str:
    """Change a thing."""
    return f"set {key}"


@mcp.tool()
def refuse() -> str:
    """Reject the call as a protocol error, as a server does with a request it won't take."""
    raise MCPError(code=INVALID_PARAMS, message="refused by the server")


@mcp.tool()
def hidden_tool() -> str:
    """A tool the broker's configuration doesn't list."""
    return "should never be reachable"


if __name__ == "__main__":
    mcp.run("stdio")
