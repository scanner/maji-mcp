"""
A gateway and a runner to test against without Docker.

`InProcessRunner` stands in for the Docker runner: it runs each script in this
process with the stub client, using the run's token, exactly as a script in an
executor would. Everything else is real: the broker's sockets, the runner's socket,
and the gateway served over HTTP to an MCP client.
"""

# system imports
import asyncio
import contextlib
import hashlib
import io
from collections.abc import AsyncIterator, Callable, Collection, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

# 3rd party imports
import uvicorn
from mcp import ClientSession

# project imports
from maji_mcp.gateway.server import build_app, build_server
from maji_mcp.runner.docker_runner import RunnerStopping, RunResult
from maji_mcp.runner.sandbox import Cover, Limits, Mount, SandboxSettings
from tests.support.services import Services

# The gateway's two principals in these tests, each with its own token: `reader`,
# whose policy is narrow, and `keeper`, who may use the maji stubs and skills.
#
READER_TOKEN = "gateway-test-token"
KEEPER_TOKEN = "gateway-keeper-token"


# Two scripts that show whether saved values carry from one run to another: the first
# saves a value, and the second prints it, or the code of the error it was given.
#
SAVE_N = "maji.save('n', [1, 2, 3])"
LOAD_N = """
try:
    print(maji.load('n'))
except maji.StubError as exc:
    print(exc.code)
"""

# What the `connected` fixture hands a test: a function that opens an MCP session for
# a token's principal, for a `with` block of the test's own.
#
type Connect = Callable[[str], AbstractAsyncContextManager[ClientSession]]


async def printed_by(session: ClientSession, code: str) -> str:
    """
    Run a script with run_python in an MCP session, and return what the script
    printed.
    """
    result = await session.call_tool("run_python", {"code": code})
    text: str = result.content[0].text  # type: ignore[union-attr]
    assert not result.is_error, text

    # The last line is the run's status; what the script printed is above it.
    #
    return text.rpartition("\n")[0]


class InProcessRunner:
    """
    Runs a script in this process, with the stub client the executor would have.

    Attributes:
        tokens: the token of each run it was given, in order
        limits: each run's limits
        mounts: each run's mounts, with the paths it was told were granted
        covers: each run's covers
        fail: set to True, it fails as Docker would when a run can't start
        stopping: set to True, it refuses a run as a runner told to stop does
    """

    def __init__(self, services: Services) -> None:
        self.services = services
        self.settings = SandboxSettings()
        self.tokens: list[str] = []
        self.limits: list[Limits] = []
        self.mounts: list[tuple[list[Mount], set[str]]] = []
        self.covers: list[list[Cover]] = []
        self.fail = False
        self.stopping = False

    def run(
        self,
        runtime: str,
        code: str,
        run_id: str,
        token: str,
        limits: Limits,
        mounts: Sequence[Mount] = (),
        granted: Collection[str] = (),
        covers: Sequence[Cover] = (),
    ) -> RunResult:
        """
        Record the run, then run its script here, with the signature of the Docker
        runner's `run`. Every run reports exit 0 and a duration of 3 ms.
        """
        if self.fail:
            raise RuntimeError("docker went away")
        if self.stopping:
            raise RunnerStopping()
        self.tokens.append(token)
        self.limits.append(limits)
        self.mounts.append((list(mounts), set(granted)))
        self.covers.append(list(covers))
        client = self.services.stub_client(token)
        namespace: dict[str, Any] = {
            "maji": client,
            "skills": client.skills,
            **client.namespaces(),
        }
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(compile(code, "<run_python>", "exec"), namespace)
        return RunResult(0, out.getvalue(), "", 3, False, False)


@contextlib.asynccontextmanager
async def serving_gateway(
    services: Services, runner_sock: Path, port: int
) -> AsyncIterator[str]:
    """Serve a new gateway over HTTP on `port`, and yield its MCP endpoint."""
    mcp = build_server(runner_sock, services.control_sock)
    hashes = {
        "reader": hashlib.sha256(READER_TOKEN.encode()).hexdigest(),
        "keeper": hashlib.sha256(KEEPER_TOKEN.encode()).hexdigest(),
    }
    app = build_app(mcp, hashes)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await task
