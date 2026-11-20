"""
JSON-RPC 2.0 over Unix sockets, one JSON message per line.

Every maji-mcp service talks to the others this way: the executor to the broker, the
runner and the gateway to the broker's control socket, and the gateway to the runner.
The protocol is language-neutral, so a client in any runtime needs only a socket, a
JSON library and a newline (`docs/design/planned/languages.md`).

Errors carry the server contract's codes (`docs/design/server-contract.md` A2) in
`error.data.code`, because JSON-RPC itself only has numeric codes. The broker adds three
of its own for the decisions it makes before a call reaches a service:
`unauthenticated`, `permission_denied` and `rate_limited`.
"""

# system imports
import asyncio
import contextlib
import json
import os
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

# The largest message accepted, in bytes. A stub result larger than this is an error,
# and a script that needs more should ask for less: a filter, a page or a projection.
#
MAX_MESSAGE = 16 * 1024 * 1024

# JSON-RPC's own codes, and the one used for every maji-mcp error.
#
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
APPLICATION_ERROR = -32000

# The server contract's codes (A2), plus the broker's own.
#
ERROR_CODES = frozenset(
    {
        "conflict",
        "not_found",
        "invalid_argument",
        "unavailable",
        "not_loaded",
        "stale",
        "internal",
        "unauthenticated",
        "permission_denied",
        "rate_limited",
    }
)


###############################################################################
#
class UnknownMethod(Exception):
    """Raised by a handler for a method it doesn't serve."""


class MajiError(Exception):
    """
    An error with one of the contract's codes, returned to the caller as data.

    Args:
        code: one of `ERROR_CODES`
        message: what went wrong, for the agent reading it
        data: anything that helps the caller act on it, such as the current version on a
            `conflict`
    """

    def __init__(
        self, code: str, message: str, data: dict[str, Any] | None = None
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}

    def to_error(self) -> dict[str, Any]:
        """Return the JSON-RPC `error` object."""
        return {
            "code": APPLICATION_ERROR,
            "message": self.message,
            "data": {"code": self.code, **self.data},
        }

    @classmethod
    def from_error(cls, error: dict[str, Any]) -> MajiError:
        """Rebuild the error a server returned."""
        data = dict(error.get("data") or {})
        code = data.pop("code", "internal")
        if code not in ERROR_CODES:
            code = "internal"
        return cls(code, str(error.get("message", "")), data)


Handler = Callable[[str, dict[str, Any]], Awaitable[Any]]


###############################################################################
#
async def _answer(raw: bytes, handler: Handler) -> dict[str, Any] | None:
    """
    Decode one request, run it, and build the response.

    Returns:
        The response object, or None for a notification, which gets no reply.
    """
    try:
        request = json.loads(raw)
    except ValueError:
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": PARSE_ERROR, "message": "not JSON"},
        }
    if not isinstance(request, dict) or not isinstance(request.get("method"), str):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": INVALID_REQUEST, "message": "not a JSON-RPC request"},
        }
    request_id = request.get("id")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    try:
        result = await handler(request["method"], params)
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "result": result}
    except MajiError as exc:
        reply = {"jsonrpc": "2.0", "id": request_id, "error": exc.to_error()}
    except UnknownMethod as exc:
        reply = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": METHOD_NOT_FOUND, "message": f"no method {exc}"},
        }
    except Exception as exc:  # every failure must reach the caller, by type only
        reply = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": MajiError("internal", type(exc).__name__).to_error(),
        }
    return None if "id" not in request else reply


async def serve_unix(path: Path, handler: Handler, mode: int = 0o600) -> asyncio.Server:
    """
    Serve JSON-RPC on a Unix socket, one request per line, many per connection.

    Args:
        path: the socket file. A stale one left by a crash is replaced.
        handler: called with the method name and params for each request. It raises
            `MajiError` for a failure the caller should see, and `UnknownMethod` for a
            method it doesn't serve.
        mode: the socket file's permissions. Whoever can open the file can connect, so
            this, and which containers mount its directory, decide who may call.

    Returns:
        The running server.
    """
    with contextlib.suppress(FileNotFoundError):
        path.unlink()

    async def connection(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while raw := await reader.readline():
                reply = await _answer(raw, handler)
                if reply is not None:
                    writer.write(json.dumps(reply).encode() + b"\n")
                    await writer.drain()
        except ConnectionError, asyncio.LimitOverrunError, ValueError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_unix_server(
        connection, path=str(path), limit=MAX_MESSAGE
    )
    os.chmod(path, mode)
    return server


###############################################################################
#
async def call_unix(
    path: Path, method: str, params: dict[str, Any], timeout: float = 60.0
) -> Any:
    """
    Make one JSON-RPC call over a Unix socket, from async code.

    Raises:
        MajiError: the server returned an error, or couldn't be reached
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path), limit=MAX_MESSAGE), timeout
        )
    except (OSError, TimeoutError) as exc:
        raise MajiError("unavailable", f"can't reach {path.name}: {exc}") from exc
    try:
        request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout)
    except TimeoutError as exc:
        raise MajiError("unavailable", f"{method} timed out") from exc
    finally:
        writer.close()
    return _result(raw, method)


def call_unix_sync(
    path: Path, method: str, params: dict[str, Any], timeout: float = 60.0
) -> Any:
    """
    Make one JSON-RPC call over a Unix socket, from blocking code.

    Raises:
        MajiError: the server returned an error, or couldn't be reached
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(str(path))
            request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            sock.sendall(json.dumps(request).encode() + b"\n")
            with sock.makefile("rb") as stream:
                raw = stream.readline(MAX_MESSAGE)
        except OSError as exc:
            raise MajiError("unavailable", f"can't reach {path.name}: {exc}") from exc
    return _result(raw, method)


def _result(raw: bytes, method: str) -> Any:
    if not raw:
        raise MajiError("unavailable", f"{method}: the connection closed early")
    reply = json.loads(raw)
    if "error" in reply:
        error = reply["error"]
        if error.get("code") == APPLICATION_ERROR:
            raise MajiError.from_error(error)
        raise MajiError("internal", f"{method}: {error.get('message')}")
    return reply.get("result")
