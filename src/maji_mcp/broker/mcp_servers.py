"""
Stubs for MCP servers behind the broker, such as emacs-org-mcp through the bridge.

Which tools become stubs is the broker's decision, written in its configuration, not the
server's (`docs/design/server-contract.md` A6). Each server lists the tools to
expose and whether each reads or writes:

    {
      "servers": {
        "org": {
          "url": "http://host.docker.internal:8766/mcp",
          "token_file": "/run/secrets/org_bridge_token",
          "version": "1.0",
          "adapter": "emacs-org",
          "tools": {"get_task": "read", "list_tasks": "read", "update_task": "write"}
        }
      }
    }

The key, `org`, is the stub namespace, so `get_task` becomes `org.get_task`. A tool
the server offers but the configuration doesn't list is never exposed, and its
description never reaches `read_api`. A tool listed as `write` becomes a stub the broker
refuses until changesets are built (`docs/design/planned/writes.md`). Approving a
server and its tools from the queue is planned to replace this file
(`docs/design/planned/queue.md`).

An `adapter` adds stubs written for that server over its listed reads: `emacs-org`
adds `org.tasks` and `org.task`, which parse emacs-org-mcp's text into records
(`emacs_org.py`).

Every call carries the caller in `_meta`, as `maji-mcp/principal` and
`maji-mcp/run_id`, from the broker's record of the run (A8).
"""

# system imports
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

# 3rd party imports
import httpx2
from mcp import ClientSession, MCPError, types
from mcp.client.streamable_http import streamable_http_client

# project imports
from maji_mcp.broker.catalog import REQUIRED, CallContext, Param, Stub
from maji_mcp.common.protocol import ERROR_CODES, MajiError

# Stubs written for a particular server, named by a server's `adapter`.
#
ADAPTERS = ("emacs-org",)

# JSON Schema types, as stub parameter types.
#
SCHEMA_TYPES = {
    "string": "str",
    "integer": "int",
    "boolean": "bool",
    "number": "float",
    "object": "dict",
}


###############################################################################
#
@dataclass
class ServerConfig:
    """One MCP server behind the broker."""

    namespace: str
    url: str
    token: str = field(repr=False)
    tools: dict[str, str]
    version: str = "1.0"
    adapter: str | None = None


def load_config(path: Path) -> list[ServerConfig]:
    """
    Read the servers configuration.

    Raises:
        ValueError: a tool classified as anything but `read` or `write`, or an
            unknown adapter
    """
    data = json.loads(path.read_text())
    servers = []
    for namespace, entry in data.get("servers", {}).items():
        tools = dict(entry.get("tools", {}))
        bad = {t: kind for t, kind in tools.items() if kind not in ("read", "write")}
        if bad:
            raise ValueError(f"{namespace}: classify each tool as read or write: {bad}")
        adapter = entry.get("adapter")
        if adapter not in (None, *ADAPTERS):
            raise ValueError(f"{namespace}: unknown adapter {adapter!r}")
        token = Path(entry["token_file"]).read_text().strip()
        servers.append(
            ServerConfig(
                namespace,
                entry["url"],
                token,
                tools,
                entry.get("version", "1.0"),
                adapter,
            )
        )
    return servers


###############################################################################
#
class McpServer:
    """
    The broker's client for one MCP server over streamable HTTP.

    A session is opened per call. Calls are rare next to an agent's turn, and this
    keeps a restarted server from leaving the broker with a dead session.

    Args:
        config: the server's configuration
        transport: replaces the network, in tests
    """

    def __init__(
        self, config: ServerConfig, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self.config = config
        self._transport = transport

    def _http(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self.config.token}"},
            timeout=httpx2.Timeout(60, read=120),
            transport=self._transport,
        )

    async def list_tools(self) -> list[types.Tool]:
        async with (
            self._http() as http,
            streamable_http_client(self.config.url, http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return (await session.list_tools()).tools

    async def call(self, tool: str, args: dict[str, Any], ctx: CallContext) -> Any:
        """
        Call a tool, and return its structured content, or its text.

        Raises:
            MajiError: the tool's own error code when it returns the contract's
                structured error (A2), otherwise `internal`; `unavailable` when the
                server can't be reached or answers with an HTTP error, such as a
                refused token
        """
        # The SDK's meta type admits any key (PEP 728's `extra_items`), which mypy
        # can't check yet, so the dict is cast to it.
        #
        meta = cast(
            types.RequestParamsMeta,
            {"maji-mcp/principal": ctx.principal, "maji-mcp/run_id": ctx.run_id},
        )

        # The SDK's client runs in task groups, so a failure arrives wrapped in
        # exception groups, which `except*` looks inside.
        #
        try:
            async with (
                self._http() as http,
                streamable_http_client(self.config.url, http_client=http) as (
                    read,
                    write,
                ),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                result = await session.call_tool(tool, args, meta=meta)
        except* (httpx2.HTTPError, OSError) as group:
            raise MajiError(
                "unavailable",
                f"{self.config.namespace} unreachable: {type(_first(group)).__name__}",
            ) from None
        except* MCPError as group:
            # The SDK reports an HTTP error status, such as 401 for a refused token,
            # as a JSON-RPC error on the call, the same way as a protocol error from
            # the server. Either way, the tool didn't run.
            #
            error = cast(MCPError, _first(group))
            raise MajiError(
                "unavailable", f"{self.config.namespace}: {error.message}"[:2000]
            ) from None
        text = "\n".join(
            block.text
            for block in result.content
            if isinstance(block, types.TextContent)
        )
        if result.is_error:
            structured = result.structured_content or {}
            code = structured.get("code")
            raise MajiError(
                code if code in ERROR_CODES else "internal",
                str(structured.get("message") or text)[:2000],
            )
        return (
            result.structured_content if result.structured_content is not None else text
        )


def _first(group: BaseExceptionGroup[Any]) -> BaseException:
    """Return the first exception in a group, looking inside any nested groups."""
    found: BaseException = group
    while isinstance(found, BaseExceptionGroup):
        found = found.exceptions[0]
    return found


def _params(schema: dict[str, Any]) -> dict[str, Param]:
    """Turn a tool's JSON input schema into stub parameters, required ones first."""
    required = set(schema.get("required", []))
    params: dict[str, Param] = {}
    properties = schema.get("properties", {})
    for name in sorted(properties, key=lambda n: n not in required):
        prop = properties[name]
        kind = prop.get("type")
        if kind == "array":
            ptype = "list[str]"
        else:
            ptype = SCHEMA_TYPES.get(kind if isinstance(kind, str) else "", "str")
        description = str(prop.get("description", "")).split("\n")[0][:200]
        default = REQUIRED if name in required else prop.get("default")
        params[name] = Param(ptype, description, default)
    return params


async def stubs(server: McpServer) -> list[Stub]:
    """
    Return a stub for each tool the configuration lists and the server offers.

    Listed tools the server doesn't offer are skipped, so a misspelt name shows up as a
    missing stub rather than a broken one.
    """
    offered = {tool.name: tool for tool in await server.list_tools()}
    found = []
    for name, kind in sorted(server.config.tools.items()):
        tool = offered.get(name)
        if tool is None:
            continue

        def handler(tool_name: str) -> Any:
            async def call(args: dict[str, Any], ctx: CallContext) -> Any:
                clean = {k: v for k, v in args.items() if v is not None}
                return await server.call(tool_name, clean, ctx)

            return call

        summary = (tool.description or name).strip().split("\n")[0][:200]
        found.append(
            Stub(
                f"{server.config.namespace}.{name}",
                summary,
                _params(tool.input_schema),
                "dict | str: the tool's structured content, or its text",
                handler(name),
                writes=kind == "write",
            )
        )
    return found
