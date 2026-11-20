"""
Tests for the relay, which serves the gateway over stdio for apps such as Claude
Desktop.

The relay is driven as the app drives it, by an MCP client on a pair of message
streams in place of stdin and stdout, and relays to the gateway served over HTTP as
in `tests/gateway/test_server.py`.
"""

# system imports
import contextlib
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

# 3rd party imports
import anyio
import httpx2
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_client_server_memory_streams
from pytest_check import check

# project imports
from maji_mcp.gateway.server import INSTRUCTIONS
from maji_mcp.host.relay import (
    Gateway,
    GatewayUnreachable,
    TokenFile,
    checked_url,
    serve,
)
from tests.support.gateway import (
    KEEPER_TOKEN,
    LOAD_N,
    SAVE_N,
    printed_by,
    serving_gateway,
)
from tests.support.services import Services

type Relayed = Callable[[str], AbstractAsyncContextManager[ClientSession]]


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    """A token file holding the keeper's token, as `maji-setup` leaves one."""
    path = tmp_path / "keeper.token"
    path.write_text(KEEPER_TOKEN + "\n")
    return path


@pytest.fixture
async def relay_to(token_file: Path) -> AsyncIterator[Callable[..., Gateway]]:
    """
    A function that returns a relay's connection to the gateway at a URL, closed after
    the test. It sends the keeper's token, or the token in another file if one is
    given.
    """
    made: list[Gateway] = []

    def connect(url: str, path: Path = token_file) -> Gateway:
        made.append(Gateway(url, path))
        return made[-1]

    yield connect
    for relay in made:
        await relay.aclose()


@pytest.fixture
def relayed(relay_to: Callable[..., Gateway]) -> Relayed:
    """
    A function that starts a relay to the gateway at a URL, as the keeper, and gives
    the app's session with it, initialized, for as long as the block runs.
    """

    # The relay runs in a task group, which has to be entered and left in one task,
    # and a fixture is set up in one task and torn down in another. So the test
    # starts the relay itself, in a block of its own.
    #
    @contextlib.asynccontextmanager
    async def start(url: str) -> AsyncIterator[ClientSession]:
        relay = relay_to(url)
        async with (
            create_client_server_memory_streams() as (app_side, relay_side),
            anyio.create_task_group() as tasks,
        ):
            tasks.start_soon(serve, *relay_side, relay)
            async with ClientSession(*app_side) as session:
                await session.initialize()
                yield session
            tasks.cancel_scope.cancel()

    return start


async def test_the_relay_serves_the_gateway_as_a_command_the_app_starts(
    gateway: str, token_file: Path
) -> None:
    """
    GIVEN: An app that starts the relay as a command, as Claude Desktop does, with
           the keeper's token file
     WHEN: the app speaks MCP on the command's stdin and stdout: it initializes, lists
           the tools, and runs a script
     THEN: it gets the gateway's own instructions and tools, as an app connected to
           the gateway directly would, and the script's output
    """
    # The app starts the relay as a command, and speaks MCP on its stdin and stdout.
    #
    command = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "maji_mcp.host.relay",
            "--token-file",
            str(token_file),
            "--url",
            gateway,
        ],
    )
    async with (
        stdio_client(command) as (read, write),
        ClientSession(read, write) as session,
    ):
        result = await session.initialize()
        tools = await session.list_tools()
        output = await printed_by(session, "print(6 * 7)")

    # What the app gets is the gateway's own: its instructions, its tools, and the
    # script's output.
    #
    with check:
        assert result.instructions == INSTRUCTIONS
    with check:
        assert {"run_python", "list_api", "save_skill"} <= {t.name for t in tools.tools}
    with check:
        assert output == "42"


async def test_every_call_through_one_relay_is_in_one_session(
    gateway: str, relayed: Relayed
) -> None:
    """
    GIVEN: An app whose relay runs for as long as the app does
     WHEN: one script saves a value, a later script through the same relay loads it,
           and a script through a second relay tries to
     THEN: the later script gets the value: one relay is one maji session
      AND: the second relay's script gets not_found, being in a session of its own
    """
    async with relayed(gateway) as session:
        assert await printed_by(session, SAVE_N) == ""
        assert await printed_by(session, LOAD_N) == "[1, 2, 3]"
    async with relayed(gateway) as session:
        assert await printed_by(session, LOAD_N) == "not_found"


async def test_a_relay_outlives_a_gateway_restart(
    services: Services,
    runner_socket: Path,
    unused_tcp_port: int,
    relayed: Relayed,
) -> None:
    """
    GIVEN: A relay whose gateway stops, and is started again on the same port
     WHEN: the app calls a tool while the gateway is down, and again once it's back
     THEN: the call made while it was down fails with an error result saying the
           gateway isn't answering, and the call after it works, with no restart of
           the app
    """
    # The first gateway is held on a stack of its own, so it can be stopped while the
    # relay, entered after it, keeps running.
    #
    first = contextlib.AsyncExitStack()
    url = await first.enter_async_context(
        serving_gateway(services, runner_socket, unused_tcp_port)
    )
    async with relayed(url) as session:
        assert await printed_by(session, "print(1)") == "1"
        await first.aclose()

        down = await session.call_tool("run_python", {"code": "print(2)"})
        assert down.is_error
        assert "the gateway isn't answering" in down.content[0].text  # type: ignore[union-attr]

        async with serving_gateway(services, runner_socket, unused_tcp_port):
            assert await printed_by(session, "print(3)") == "3"


async def test_a_relay_whose_gateway_is_down_says_so_before_it_starts(
    relay_to: Callable[..., Gateway], unused_tcp_port: int
) -> None:
    """
    GIVEN: An app that starts its relay before the stack is up
     WHEN: the relay asks the gateway what it serves
     THEN: it is refused at once, saying the gateway isn't answering, so the app shows
           the server as failed to start instead of one with no tools
    """
    relay = relay_to(f"http://127.0.0.1:{unused_tcp_port}/mcp")
    with pytest.raises(GatewayUnreachable, match="the gateway isn't answering"):
        await relay.discover()


async def test_a_relay_with_a_token_the_gateway_refuses_doesnt_start(
    gateway: str,
    relay_to: Callable[..., Gateway],
    new_token: Callable[[], str],
    tmp_path: Path,
) -> None:
    """
    GIVEN: A token file holding a token the gateway doesn't know
     WHEN: the relay asks the gateway what it serves
     THEN: it is refused, saying which file's token the gateway refused and how to
           get a new one, without the token itself
    """
    unknown = new_token()
    path = tmp_path / "old.token"
    path.write_text(unknown)
    with pytest.raises(GatewayUnreachable) as caught:
        await relay_to(gateway, path).discover()
    with check:
        assert f"the gateway refused the token in {path}" in str(caught.value)
    with check:
        assert "make reconnect-claude-code" in str(caught.value)
    with check:
        assert unknown not in str(caught.value)


def test_the_token_is_read_from_its_file_for_every_request(
    new_token: Callable[[], str], tmp_path: Path
) -> None:
    """
    GIVEN: A relay's token file, replaced while the relay runs, as
           `make reconnect-claude-code` replaces it
     WHEN: the relay sends a request before and after
     THEN: each request carries the token the file held when it was sent
      AND: a file holding anything but a token's characters sends nothing, and the
           error doesn't repeat what the file holds
    """
    path = tmp_path / "claude-code.token"
    auth = TokenFile(path)

    def sent() -> str:
        """The Authorization header of a request the relay sends now."""
        request = httpx2.Request("POST", "http://127.0.0.1:8765/mcp")
        return next(auth.auth_flow(request)).headers["Authorization"]

    first, second = new_token(), new_token()
    path.write_text(f"{first}\n")
    assert sent() == f"Bearer {first}"
    path.write_text(f"{second}\n")
    assert sent() == f"Bearer {second}"

    path.write_text('x", "X-Maji-Session": "fixed')
    with pytest.raises(ValueError) as caught:
        sent()
    assert "fixed" not in str(caught.value)


@pytest.mark.parametrize(
    ("url", "taken"),
    [
        ("http://127.0.0.1:8765/mcp", True),
        ("http://localhost:8765/mcp", True),
        ("http://[::1]:8765/mcp", True),
        ("https://example.com/mcp", False),
        ("http://10.0.0.1:8765/mcp", False),
        ("http://127.0.0.1.example.com/mcp", False),
        ("https://127.0.0.1:8765/mcp", False),
        ("http://user@example.com/mcp", False),
    ],
)
def test_only_a_url_on_this_machine_is_taken(url: str, taken: bool) -> None:
    """
    GIVEN: A URL for the gateway
     WHEN: the relay is given it
     THEN: If it is plain HTTP on this machine's loopback address, it is taken as it
           is
      AND: If it names another host, or another scheme, it is refused, so the token
           the relay sends with every request stays on this machine
    """
    if taken:
        assert checked_url(url) == url
    else:
        with pytest.raises(ValueError, match="isn't the gateway on this machine"):
            checked_url(url)
