"""
The bridge: serves a stdio MCP server that acts as you over HTTP, for the broker.

Servers such as emacs-org-mcp run as you, on your files, and speak MCP over stdio. The
broker runs in a container on the container VM's kernel, which can't reach a Unix socket
or a process on the Mac, but can reach the Mac's `127.0.0.1` through
`host.docker.internal` (`docs/design/server-contract.md` B2). The bridge closes that gap
without changing the server:

- it starts the server as a child process, as you, and keeps one MCP session open
- it serves the same tools over streamable HTTP on `127.0.0.1` only, behind a bearer
  token read from a file only you can read: it makes the file that way, narrows the
  mode of one it finds readable by others, and doesn't start on a file with no token
- it passes each call through unchanged, one at a time, with the caller's `_meta`
  (`maji-mcp/principal`, `maji-mcp/run_id`) for the server's logs
- it writes a registration file for the broker while it runs (B4)

The bridge doesn't decide what the broker may call. The broker exposes only the tools
its own configuration lists, so a server adding a tool changes nothing until you list
it there (B5).

Run it with:

    maji-bridge --id emacs-org --port 8766 -- \\
        uv --directory ~/src/emacs-org-mcp run server.py --no-ediff-approval
"""

# system imports
import argparse
import asyncio
import contextlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

# 3rd party imports
import uvicorn
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server import Server, ServerRequestContext
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

# project imports
from maji_mcp.common.stopping import until_stopped

CONTRACT = "0.1"
CONFIG_DIR = Path.home() / ".config" / "maji-mcp"

# What a server's id may be. The id names the server's token file, `<id>.token`, and
# its registration, `<id>.json`, so it holds nothing that could name a file anywhere
# but in the directories those are kept in.
#
SERVER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


###############################################################################
#
def checked_server_id(server_id: str) -> str:
    """
    Return a server's id, once it is known to name only files of the server's own.

    Raises:
        ValueError: the id isn't 1 to 64 letters, digits, hyphens and underscores
            starting with a letter or a digit
    """
    if not SERVER_ID.fullmatch(server_id):
        raise ValueError(
            f"{server_id!r} can't be a server's id: an id is 1 to 64 letters, "
            "digits, hyphens and underscores, and starts with a letter or a digit"
        )
    return server_id


class TokenFileError(Exception):
    """The bridge's token file can't be used as it is. The message says why."""


def read_or_create_token(path: Path) -> str:
    """
    Return the bridge's token from its file, which only you can read.

    A file that doesn't exist is created with a new token. A file that exists is
    kept, and if anyone else could read it, its mode is narrowed to yours alone.

    Raises:
        TokenFileError: the file holds no token, or its mode can't be narrowed
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_urlsafe(32))
    elif stat.S_IMODE(path.stat().st_mode) & 0o077:
        try:
            path.chmod(0o600)
        except OSError as exc:
            raise TokenFileError(
                f"{path} is readable by others, and its mode can't be changed: {exc}"
            ) from exc

    # An empty token would make `Bearer ` with nothing after it the whole of what a
    # request has to send, so the bridge serves only with a token to ask for.
    #
    token = path.read_text().strip()
    if not token:
        raise TokenFileError(
            f"{path} holds no token: remove the file, and the bridge makes a new one"
        )
    return token


class BearerAuth:
    """
    ASGI middleware admitting only requests that bear the bridge's token.

    Args:
        app: the MCP app to protect
        token: the token, compared in constant time
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        # Only the server's own startup and shutdown pass unchecked; any other scope
        # that isn't HTTP, such as a websocket, is refused.
        #
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        headers = dict(scope.get("headers") or [])
        if not hmac.compare_digest(headers.get(b"authorization", b""), self._expected):
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)


###############################################################################
#
def proxy_server(session: ClientSession, name: str) -> Server[Any]:
    """
    Build an MCP server whose tools are those of `session`, passed through.

    Calls are sent one at a time, because a server acting on your files, like
    emacs-org-mcp, may not expect two writes at once (`concurrency: 1` in the
    registration). A call that fails as a whole, such as one the stdio server rejects
    as a protocol error, comes back as an error result carrying the failure's message,
    as the server's own tool errors do.
    """
    lock = asyncio.Lock()

    async def list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=(await session.list_tools()).tools)

    async def call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        # The SDK's meta type admits any key (PEP 728's `extra_items`), which mypy
        # can't check yet, so the dict is cast to it.
        #
        meta = ctx.meta or {}
        forwarded = cast(
            types.RequestParamsMeta,
            {k: v for k, v in meta.items() if k.startswith("maji-mcp/")},
        )
        try:
            async with lock:
                return await session.call_tool(
                    params.name, params.arguments or {}, meta=forwarded or None
                )
        except Exception as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc))],
                is_error=True,
            )

    return Server(name, on_list_tools=list_tools, on_call_tool=call_tool)


@contextlib.asynccontextmanager
async def bridged(command: list[str]) -> AsyncIterator[ClientSession]:
    """Start the stdio server, and yield an initialized session with it."""
    params = StdioServerParameters(
        command=command[0], args=command[1:], env=dict(os.environ)
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def registration(
    server_id: str, name: str, port: int, token_file: Path
) -> dict[str, Any]:
    """The registration file's content (`docs/design/server-contract.md` B4)."""
    return {
        "id": server_id,
        "name": name,
        "contract": CONTRACT,
        "transport": "http",
        "url": f"http://127.0.0.1:{port}/mcp",
        "token_file": str(token_file),
        "concurrency": 1,
    }


async def serve(
    server_id: str,
    command: list[str],
    port: int,
    token_file: Path,
    registry: Path,
    started: asyncio.Event | None = None,
) -> None:
    """
    Run the bridge until cancelled, registering it while it serves.

    Args:
        server_id: the id the broker's configuration knows this server by
        command: the stdio server's command line
        port: the port on `127.0.0.1`
        token_file: the bearer token's file, created if missing
        registry: the directory the registration file goes in
        started: set once the bridge is serving, for tests

    Raises:
        ValueError: the id can't name the server's files, before anything is read
        TokenFileError: the token file can't be used, before the server is started
    """
    checked_server_id(server_id)
    token = read_or_create_token(token_file)
    async with bridged(command) as session:
        mcp = proxy_server(session, f"maji-bridge:{server_id}")
        manager = StreamableHTTPSessionManager(
            app=mcp,
            stateless=True,
            security_settings=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=["127.0.0.1:*", "localhost:*", "host.docker.internal:*"],
                allowed_origins=[],
            ),
        )

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
                await manager.handle_request(scope, receive, send)
            elif scope["type"] == "http":
                await send(
                    {"type": "http.response.start", "status": 404, "headers": []}
                )
                await send({"type": "http.response.body", "body": b""})

        config = uvicorn.Config(
            BearerAuth(app, token),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="off",
        )
        http = uvicorn.Server(config)
        registry.mkdir(parents=True, exist_ok=True, mode=0o700)
        record = registry / f"{server_id}.json"
        async with manager.run():
            task = asyncio.create_task(http.serve())
            try:
                while not http.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
                record.write_text(
                    json.dumps(
                        registration(server_id, server_id, port, token_file), indent=2
                    )
                )

                # Said once it answers, so a bridge started by hand, or the log of
                # one started in the background, shows that it is up and where.
                #
                print(
                    f"bridge: serving {server_id} at http://127.0.0.1:{port}/mcp",
                    file=sys.stderr,
                    flush=True,
                )
                if started:
                    started.set()
                await task
            finally:
                record.unlink(missing_ok=True)
                http.should_exit = True
                with contextlib.suppress(asyncio.CancelledError):
                    await task


###############################################################################
#
def server_id_argument(value: str) -> str:
    """
    Check a server's id as argparse reads it, so an id that can't be one is a usage
    error before the bridge reads or writes a file.

    Raises:
        argparse.ArgumentTypeError: saying what an id may be
    """
    try:
        return checked_server_id(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="serve a stdio MCP server over HTTP")
    parser.add_argument(
        "--id",
        required=True,
        type=server_id_argument,
        help="the server's id, such as emacs-org",
    )
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--token-file", type=Path, help="default: ~/.config/maji-mcp/<id>.token"
    )
    parser.add_argument("--registry", type=Path, default=CONFIG_DIR / "servers")
    parser.add_argument(
        "command", nargs=argparse.REMAINDER, help="-- then the server's command"
    )
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give the stdio server's command after --")
    token_file = args.token_file or CONFIG_DIR / f"{args.id}.token"
    try:
        asyncio.run(
            until_stopped(serve(args.id, command, args.port, token_file, args.registry))
        )
    except TokenFileError as exc:
        sys.exit(f"bridge: {exc}")
    print("bridge: stopped", file=sys.stderr)


if __name__ == "__main__":
    main()
