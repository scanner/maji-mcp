"""
The relay: serves maji-mcp's gateway over stdio, for apps that start MCP servers as
commands.

Claude Desktop starts each local MCP server in its configuration as a command and
speaks MCP over the command's stdin and stdout. Its connectors for remote servers
connect from Anthropic's cloud, which can't reach the gateway on 127.0.0.1. The
relay is the command (`make connect-claude-desktop` adds it):

- it serves the gateway's instructions and tools over stdio, and passes each call on
  to the gateway over HTTP
- it reads the token from its file for every request, so the token never sits in the
  app's configuration, and a token replaced by `make reconnect-claude-code` is used
  from the next request on
- it speaks the 2026-07-28 revision, which has no MCP sessions, and names one session
  in `X-Maji-Session` for as long as it runs, so a gateway restarted under it begins
  a fresh session instead of refusing every later call

One relay is one maji session. Claude Desktop keeps one relay running for as long as
it runs, so every chat in it shares that session's grants and saved values, until
Desktop quits or the session goes an hour without a script (`NamedSessions` in
`gateway/server.py`).

    maji-relay --token-file ~/.config/maji-mcp/secrets/claude-code.token \
        --url http://127.0.0.1:8765/mcp
"""

# system imports
import argparse
import contextlib
import re
import secrets
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Generator
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# 3rd party imports
import anyio
import httpx2
from mcp import Client, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.shared._httpx_utils import create_mcp_http_client

DEFAULT_URL = "http://127.0.0.1:8765/mcp"
SESSION_HEADER = "X-Maji-Session"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
TOKEN = re.compile(r"[A-Za-z0-9_-]+")

# The gateway waits up to 700 seconds for a run (a script may take 600), so the relay
# waits a little longer for the gateway's answer.
#
READ_TIMEOUT_S = 720.0


class GatewayUnreachable(Exception):
    """The gateway didn't answer, or answered as something other than the gateway."""


class TokenRefused(Exception):
    """The gateway refused the token in the relay's token file."""


###############################################################################
#
def checked_url(url: str) -> str:
    """
    Return `url` if it is plain HTTP on this machine's loopback address.

    The relay sends the token with every request, so it sends requests only to the
    gateway on this machine.

    Raises:
        ValueError: the URL names another host, or a scheme other than http
    """
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK:
        raise ValueError(
            f"{url} isn't the gateway on this machine: give an http:// URL on "
            "127.0.0.1, localhost or [::1]"
        )
    return url


def read_token(path: Path) -> str:
    """
    Return the token in `path`.

    Raises:
        OSError: the file can't be read
        ValueError: the file holds something other than a token
    """
    token = path.read_text().strip()
    if not TOKEN.fullmatch(token):
        raise ValueError(f"{path} doesn't hold a token")
    return token


class TokenFile(httpx2.Auth):
    """Sends the token in a file as the bearer token, read afresh for each request."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def auth_flow(
        self, request: httpx2.Request
    ) -> Generator[httpx2.Request, httpx2.Response]:
        request.headers["Authorization"] = f"Bearer {read_token(self.path)}"
        yield request


def reason(exc: BaseException) -> str:
    """
    Return what went wrong, from the first error inside any exception groups.

    A failed request is raised inside the MCP client's task groups, so the message
    worth showing is that of the error they wrap.
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if isinstance(exc, httpx2.ConnectError):
        return "the gateway isn't answering: is the stack up? (make up)"
    return str(exc) or type(exc).__name__


def refusing(token_file: Path) -> Callable[[httpx2.Response], Awaitable[None]]:
    """
    Return a response hook that raises `TokenRefused` for a 401.

    The MCP client reports every HTTP error alike, so the hook is what tells a
    refused token from other failures, and says which file held it.
    """

    async def check(response: httpx2.Response) -> None:
        if response.status_code == 401:
            raise TokenRefused(
                f"the gateway refused the token in {token_file}: "
                "`make reconnect-claude-code` gives it a new one and restarts the "
                "gateway"
            )

    return check


###############################################################################
#
class Gateway:
    """
    The gateway, reached over HTTP with a fresh MCP client for each request.

    The protocol revision has no MCP sessions, so a client holds no state worth
    keeping between requests; one per request means a request that fails, as when the
    gateway restarts during it, takes no later request with it. The requests share
    one connection pool, and the session is named by the header the pool sends.

    Args:
        url: the gateway's MCP endpoint, on this machine
        token_file: the file holding the principal's token
        session: the id named in `X-Maji-Session`; a new one when not given
    """

    def __init__(self, url: str, token_file: Path, session: str | None = None) -> None:
        self.url = checked_url(url)
        self.session = session or secrets.token_hex(16)
        self.http = create_mcp_http_client(
            headers={SESSION_HEADER: self.session},
            timeout=httpx2.Timeout(30.0, read=READ_TIMEOUT_S),
            auth=TokenFile(token_file),
        )
        self.http.event_hooks = {"response": [refusing(token_file)]}
        self.discovered: types.DiscoverResult | None = None
        self.protocol = "2026-07-28"

    @contextlib.asynccontextmanager
    async def client(self) -> AsyncIterator[Client]:
        """Yield an MCP client for one request, on the revision the gateway speaks."""
        async with Client(
            streamable_http_client(
                self.url, http_client=self.http, terminate_on_close=False
            ),
            mode=self.protocol,
            prior_discover=self.discovered,
            cache=None,
            client_info=types.Implementation(
                name="maji-relay", version=version("maji-mcp")
            ),
        ) as client:
            yield client

    async def discover(self) -> types.DiscoverResult:
        """
        Ask the gateway what it serves, for the instructions the relay passes on.

        Raises:
            GatewayUnreachable: it can't be reached, refuses the token, or doesn't
                speak a revision without MCP sessions
        """
        # A client pinned to a revision starts from a stand-in for the server's
        # answer, which `discover()` would return as it is, so the request is sent
        # directly.
        #
        try:
            async with self.client() as client:
                raw = await client.session.send_discover(self.protocol)
            discovered = types.DiscoverResult.model_validate(raw)
        except Exception as exc:
            raise GatewayUnreachable(f"{self.url}: {reason(exc)}") from exc
        if self.protocol not in discovered.supported_versions:
            raise GatewayUnreachable(
                f"{self.url} doesn't speak {self.protocol}: is it maji-mcp's gateway?"
            )
        self.discovered = discovered
        return discovered

    async def list_tools(self) -> types.ListToolsResult:
        async with self.client() as client:
            return await client.list_tools()

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        """
        Call a tool on the gateway.

        Returns:
            The gateway's result, or for a call that failed as a whole, as when the
            gateway is down, an error result saying why
        """
        try:
            async with self.client() as client:
                return await client.call_tool(name, arguments)
        except Exception as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"maji: {reason(exc)}")],
                is_error=True,
            )

    async def aclose(self) -> None:
        await self.http.aclose()


def relay_server(gateway: Gateway, instructions: str | None) -> Server[Any]:
    """Build the stdio side: the gateway's instructions and tools, passed through."""

    async def list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return await gateway.list_tools()

    async def call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        return await gateway.call_tool(params.name, params.arguments or {})

    return Server(
        "maji",
        version=version("maji-mcp"),
        instructions=instructions,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def serve(read: Any, write: Any, gateway: Gateway) -> None:
    """
    Serve the gateway on a pair of MCP message streams until the app closes them.

    Raises:
        GatewayUnreachable: the gateway can't be reached when the relay starts
    """
    discovered = gateway.discovered or await gateway.discover()
    server = relay_server(gateway, discovered.instructions)
    await server.run(read, write, server.create_initialization_options())


async def run_on_stdio(url: str, token_file: Path) -> None:
    """
    Serve the gateway on stdin and stdout, once it has answered.

    Raises:
        GatewayUnreachable: the gateway can't be reached
    """
    gateway = Gateway(url, token_file)
    try:
        await gateway.discover()
        async with stdio_server() as (read, write):
            await serve(read, write, gateway)
    finally:
        await gateway.aclose()


###############################################################################
#
def main() -> None:
    parser = argparse.ArgumentParser(
        description="serve maji-mcp's gateway over stdio, for apps such as Claude "
        "Desktop that start MCP servers as commands"
    )
    parser.add_argument(
        "--token-file", type=Path, required=True, help="the principal's token file"
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="the gateway's endpoint")
    args = parser.parse_args()
    try:
        checked_url(args.url)
        read_token(args.token_file)
    except (OSError, ValueError) as exc:
        sys.exit(f"maji-relay: {exc}")
    try:
        anyio.run(run_on_stdio, args.url, args.token_file)
    except GatewayUnreachable as exc:
        sys.exit(f"maji-relay: {exc}; then quit and reopen the app")


if __name__ == "__main__":
    main()
