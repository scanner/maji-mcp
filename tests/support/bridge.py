"""
The bridge serving a stdio MCP server on this machine, for a test to reach.

The bridge runs as a task in the test's own event loop. Anything that waits on it,
such as a container starting or a script running, has to leave the loop running
meanwhile.
"""

# system imports
import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

# project imports
from maji_mcp.host.bridge import serve


@dataclass(frozen=True)
class Bridged:
    """
    A bridge that is serving.

    Attributes:
        port: its port on 127.0.0.1
        token: the bearer token it asks for
        registry: the folder holding its registration file while it serves
    """

    port: int
    token: str
    registry: Path

    @property
    def url(self) -> str:
        """The bridge's MCP endpoint, as this machine reaches it."""
        return f"http://127.0.0.1:{self.port}/mcp"


@contextlib.asynccontextmanager
async def serving_bridge(
    server_id: str,
    command: list[str],
    port: int,
    folder: Path,
    *,
    within: float = 20,
) -> AsyncIterator[Bridged]:
    """
    Serve a stdio MCP server through the bridge for as long as the block runs.

    Args:
        server_id: the id the broker's configuration knows the server by
        command: the stdio server's command line
        port: a free port on 127.0.0.1
        folder: where the bridge's token file and registry go
        within: how many seconds the server may take to start

    Yields:
        Where the bridge is serving, and its token.

    Raises:
        Exception: whatever stopped the bridge, if it stopped before it was serving.
        TimeoutError: if the bridge isn't serving within `within` seconds.
    """
    started = asyncio.Event()
    token_file = folder / f"{server_id}.token"
    registry = folder / "servers"
    serving = asyncio.create_task(
        serve(server_id, command, port, token_file, registry, started)
    )
    ready = asyncio.create_task(started.wait())
    try:
        # A bridge whose server can't start ends at once, with the reason. Waiting
        # for either outcome reports that reason rather than a timeout.
        #
        await asyncio.wait(
            {serving, ready}, timeout=within, return_when=asyncio.FIRST_COMPLETED
        )
        if serving.done():
            serving.result()
        if not started.is_set():
            raise TimeoutError(f"the bridge wasn't serving within {within} seconds")
        yield Bridged(port, token_file.read_text().strip(), registry)
    finally:
        for task in (ready, serving):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
