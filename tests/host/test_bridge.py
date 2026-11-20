"""
Tests for the bridge, and through it for the broker's stubs over an MCP server behind
it (`src/maji_mcp/broker/mcp_servers.py`).

The bridge serves a fake stdio MCP server (`tests/support/fake_stdio_server.py`) over
HTTP on 127.0.0.1, as it would serve emacs-org-mcp.
"""

# system imports
import asyncio
import json
import signal
import stat
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

# 3rd party imports
import httpx
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.broker.catalog import CallContext, Catalog, Stub
from maji_mcp.broker.mcp_servers import McpServer, ServerConfig, stubs
from maji_mcp.broker.policy import Grant, Policy
from maji_mcp.common.protocol import MajiError
from maji_mcp.host.bridge import (
    BearerAuth,
    TokenFileError,
    read_or_create_token,
    serve,
)
from tests.support.bridge import Bridged, serving_bridge
from tests.support.paths import FAKE_SERVER
from tests.support.services import Services

FAKE_COMMAND = [sys.executable, str(FAKE_SERVER)]

# The tools the broker's configuration lists for the server: three of the five it
# offers, and one it doesn't have.
#
TOOLS = {
    "get_thing": "read",
    "echo_meta": "read",
    "set_thing": "write",
    "missing": "read",
}


@pytest.fixture
async def bridge(tmp_path: Path, unused_tcp_port: int) -> AsyncIterator[Bridged]:
    """
    The bridge serving the fake stdio server as `fake`, stopped after the test. Its
    token file is `fake.token` in `tmp_path`, and its registry is `servers` there.
    """
    async with serving_bridge(
        "fake", FAKE_COMMAND, unused_tcp_port, tmp_path
    ) as serving:
        yield serving


@pytest.fixture
def config(bridge: Bridged) -> ServerConfig:
    """
    The broker's configuration for the server behind the bridge: the bridge's address
    and token, and TOOLS.
    """
    return ServerConfig("fake", bridge.url, bridge.token, TOOLS)


@pytest.fixture
async def stubs_by_name(config: ServerConfig) -> dict[str, Stub]:
    """The stubs the broker builds for the server behind the bridge, by name."""
    return {stub.name: stub for stub in await stubs(McpServer(config))}


@pytest.fixture
def caller(faker: Faker) -> CallContext:
    """Who a call is made for: a made-up principal and run id."""
    return CallContext(principal=faker.user_name(), run_id=faker.hexify("^" * 32))


async def test_the_bridge_registers_itself_while_it_serves(
    bridge: Bridged, tmp_path: Path
) -> None:
    """
    GIVEN: A bridge serving a stdio server
     WHEN: it has started
     THEN: its registration file names it as an HTTP server on 127.0.0.1, with its
           token file and a concurrency of one, and the token file is yours alone
    """
    record = json.loads((bridge.registry / "fake.json").read_text())
    with check:
        assert record == {
            "id": "fake",
            "name": "fake",
            "contract": "0.1",
            "transport": "http",
            "url": bridge.url,
            "token_file": str(tmp_path / "fake.token"),
            "concurrency": 1,
        }
    with check:
        assert (tmp_path / "fake.token").stat().st_mode & 0o777 == 0o600


async def test_a_stopped_bridge_removes_its_registration(
    tmp_path: Path, unused_tcp_port: int
) -> None:
    """
    GIVEN: A running bridge
     WHEN: it stops
     THEN: its registration file is gone, so the broker sees it as down
    """
    async with serving_bridge(
        "fake", FAKE_COMMAND, unused_tcp_port, tmp_path
    ) as serving:
        record = serving.registry / "fake.json"
        assert record.exists()
    assert not record.exists()


# Ids a server can't have: each would name a file other than the server's own token
# file and registration.
#
NOT_A_SERVERS_ID = [
    pytest.param("../escaped", id="a path out of the directory"),
    pytest.param("sub/name", id="a path into a folder"),
    pytest.param("", id="empty"),
    pytest.param(".hidden", id="starting with a dot"),
    pytest.param("two words", id="holding a space"),
    pytest.param("n" * 65, id="longer than 64 characters"),
]


@pytest.mark.parametrize("server_id", NOT_A_SERVERS_ID)
async def test_an_id_that_cant_be_a_servers_serves_nothing(
    tmp_path: Path, unused_tcp_port: int, server_id: str
) -> None:
    """
    GIVEN: An id that isn't letters, digits, hyphens and underscores
     WHEN: the bridge is asked to serve a server under it
     THEN: it is refused, saying what an id may be
      AND: no token file or registration is written, in the bridge's folders or
           beside them, and the server is never started
    """
    folder = tmp_path / "bridge"
    folder.mkdir()

    with pytest.raises(ValueError, match="can't be a server's id"):
        await serve(
            server_id,
            FAKE_COMMAND,
            unused_tcp_port,
            folder / f"{server_id}.token",
            folder / "servers",
        )
    assert list(tmp_path.rglob("*")) == [folder]


def test_the_bridge_command_refuses_such_an_id_as_a_usage_error(
    tmp_path: Path, unused_tcp_port: int
) -> None:
    """
    GIVEN: The maji-bridge command
     WHEN: it is given an id that can't be a server's
     THEN: it exits as a usage error that says what an id may be, and writes nothing
    """
    ran = subprocess.run(
        [
            sys.executable,
            "-m",
            "maji_mcp.host.bridge",
            "--id",
            "../escaped",
            "--port",
            str(unused_tcp_port),
            "--registry",
            str(tmp_path / "servers"),
            "--",
            *FAKE_COMMAND,
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=30,
    )

    with check:
        assert ran.returncode == 2
    with check:
        assert "can't be a server's id: an id is 1 to 64 letters" in ran.stderr
    with check:
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("found", "left"),
    [(0o644, 0o600), (0o640, 0o600), (0o604, 0o600), (0o600, 0o600), (0o400, 0o400)],
    ids=[
        "readable by everyone",
        "readable by its group",
        "readable by others",
        "yours to read and write",
        "yours to read",
    ],
)
def test_a_token_file_the_bridge_finds_is_kept_and_made_yours_alone(
    tmp_path: Path, new_token: Callable[[], str], found: int, left: int
) -> None:
    """
    GIVEN: A token file that exists before the bridge starts, as when setup made it
           or you wrote it by hand
     WHEN: the bridge reads its token
     THEN: the token is the one the file holds
      AND: if anyone else could read the file, only you can now; a file that was
           yours alone keeps its mode
    """
    token = new_token()
    token_file = tmp_path / "fake.token"
    token_file.write_text(token + "\n")
    token_file.chmod(found)

    with check:
        assert read_or_create_token(token_file) == token
    with check:
        assert stat.S_IMODE(token_file.stat().st_mode) == left


@pytest.mark.parametrize("content", ["", " \n"], ids=["empty", "only whitespace"])
def test_a_token_file_with_no_token_is_refused(tmp_path: Path, content: str) -> None:
    """
    GIVEN: A token file that exists and holds no token
     WHEN: the bridge reads its token
     THEN: it is refused, saying which file and what to do, and the file is left as
           it was, so the bridge never serves behind a token of nothing
    """
    token_file = tmp_path / "fake.token"
    token_file.write_text(content)

    with pytest.raises(TokenFileError, match="holds no token: remove the file"):
        read_or_create_token(token_file)
    assert token_file.read_text() == content


def test_the_bridge_doesnt_start_on_a_token_file_with_no_token(
    tmp_path: Path, unused_tcp_port: int
) -> None:
    """
    GIVEN: The maji-bridge command, pointed at a token file that holds no token
     WHEN: it is run
     THEN: it exits with an error of one line naming the file, without starting the
           server or registering itself
    """
    token_file = tmp_path / "fake.token"
    token_file.write_text("")
    registry = tmp_path / "servers"

    ran = subprocess.run(
        [
            sys.executable,
            "-m",
            "maji_mcp.host.bridge",
            "--id",
            "fake",
            "--port",
            str(unused_tcp_port),
            "--token-file",
            str(token_file),
            "--registry",
            str(registry),
            "--",
            *FAKE_COMMAND,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    with check:
        assert ran.returncode == 1
    with check:
        assert ran.stderr.splitlines() == [
            f"bridge: {token_file} holds no token: remove the file, and the bridge "
            "makes a new one"
        ]
    with check:
        assert not registry.exists()


@pytest.mark.parametrize(
    "sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"]
)
async def test_a_signalled_bridge_removes_its_registration(
    tmp_path: Path, unused_tcp_port: int, sig: signal.Signals
) -> None:
    """
    GIVEN: The maji-bridge command serving a stdio server
     WHEN: it receives SIGTERM, as a service manager stops it, or SIGINT, as Ctrl-C
     THEN: it exits on its own, and its registration file is gone, so the broker sees
           it as down
      AND: what it wrote says where it served and that it stopped, and nothing of its
           token
    """
    record = tmp_path / "servers" / "fake.json"
    token_file = tmp_path / "fake.token"

    # The bridge runs as the command a service manager or a terminal starts.
    #
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "maji_mcp.host.bridge",
        "--id",
        "fake",
        "--port",
        str(unused_tcp_port),
        "--token-file",
        str(token_file),
        "--registry",
        str(record.parent),
        "--",
        *FAKE_COMMAND,
        stderr=asyncio.subprocess.PIPE,
    )

    # It registers once it is serving, and is signalled then. It has to end by
    # itself: it is killed only if it is still running when the test gives up.
    #
    said = b""
    try:
        async with asyncio.timeout(20):
            while not record.exists():
                await asyncio.sleep(0.05)
        proc.send_signal(sig)
        async with asyncio.timeout(20):
            _, said = await proc.communicate()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    # It exited cleanly, and took its registration with it.
    #
    with check:
        assert proc.returncode == 0
    with check:
        assert not record.exists()

    # What it wrote to stderr says where it served and that it stopped, and holds
    # nothing of its token.
    #
    lines = said.decode().splitlines()
    with check:
        assert (
            f"bridge: serving fake at http://127.0.0.1:{unused_tcp_port}/mcp" in lines
        )
    with check:
        assert lines[-1:] == ["bridge: stopped"]
    with check:
        assert token_file.read_text().strip() not in said.decode()


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer wrong", "bearer"],
    ids=["no token", "a wrong token", "an empty token"],
)
async def test_the_bridge_refuses_requests_without_its_token(
    bridge: Bridged, authorization: str | None
) -> None:
    """
    GIVEN: A running bridge
     WHEN: a request arrives with no token, a wrong one, or an empty one
     THEN: it answers 401, before the stdio server sees anything
    """
    headers = {"Authorization": authorization} if authorization else {}
    async with httpx.AsyncClient() as client:
        response = await client.post(bridge.url, headers=headers, json={})
    assert response.status_code == 401


@pytest.mark.parametrize("scope_type", ["websocket", "something-new"])
async def test_a_scope_that_isnt_http_never_reaches_the_app(
    new_token: Callable[[], str], scope_type: str
) -> None:
    """
    GIVEN: The bridge's token check in front of an app
     WHEN: a connection arrives that isn't HTTP, such as a websocket, carrying the
           bridge's token
     THEN: it is refused, and the app behind never sees it
    """
    token = new_token()
    inner = AsyncMock()
    scope = {
        "type": scope_type,
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    await BearerAuth(inner, token)(scope, AsyncMock(), AsyncMock())
    inner.assert_not_awaited()


async def test_the_servers_own_lifespan_passes(new_token: Callable[[], str]) -> None:
    """
    GIVEN: The bridge's token check in front of an app
     WHEN: the server starts up, which sends a lifespan scope with no token
     THEN: the app receives it, so its startup and shutdown run
    """
    inner, receive, send = AsyncMock(), AsyncMock(), AsyncMock()
    scope = {"type": "lifespan"}
    await BearerAuth(inner, new_token())(scope, receive, send)
    inner.assert_awaited_once_with(scope, receive, send)


async def test_only_the_configured_tools_become_stubs(
    stubs_by_name: dict[str, Stub],
) -> None:
    """
    GIVEN: A server offering five tools, and a broker configuration listing three of
           them plus one the server doesn't have
     WHEN: the broker builds the server's stubs
     THEN: exactly the listed tools the server offers become stubs, the write is
           marked as writing, and the unlisted tools are never exposed
    """
    with check:
        assert sorted(stubs_by_name) == [
            "fake.echo_meta",
            "fake.get_thing",
            "fake.set_thing",
        ]
    with check:
        assert stubs_by_name["fake.set_thing"].writes is True
    with check:
        assert stubs_by_name["fake.get_thing"].writes is False
    with check:
        assert stubs_by_name["fake.get_thing"].signature() == (
            "fake.get_thing(*, key: str, verbose: bool = False) -> dict | str"
        )


async def test_a_call_carries_the_callers_identity_in_meta(
    stubs_by_name: dict[str, Stub], caller: CallContext, faker: Faker
) -> None:
    """
    GIVEN: A stub over a server behind the bridge
     WHEN: the broker calls it for a run
     THEN: the server receives the run's principal and id in the request's _meta, and
           the stub returns the tool's structured content
    """
    key = faker.word()
    echoed = await stubs_by_name["fake.echo_meta"].handler({}, caller)
    thing = await stubs_by_name["fake.get_thing"].handler(
        {"key": key, "verbose": None}, caller
    )
    with check:
        assert echoed == {
            "maji-mcp/principal": caller.principal,
            "maji-mcp/run_id": caller.run_id,
        }
    with check:
        assert thing == {"key": key, "value": f"value-of-{key}", "verbose": False}


@pytest.mark.parametrize(
    "wrong",
    ["token", "url"],
    ids=["a token the bridge refuses", "an address where nothing is listening"],
)
async def test_a_server_that_wont_take_the_call_is_unavailable(
    config: ServerConfig,
    caller: CallContext,
    new_token: Callable[[], str],
    unused_tcp_port_factory: Callable[[], int],
    wrong: str,
) -> None:
    """
    GIVEN: A stub over a server behind the bridge
     WHEN: the broker calls it with a token the bridge refuses, or at an address where
           nothing is listening
     THEN: the call fails with unavailable, naming the server's namespace, so a script
           can tell an outage from a fault in the tool
    """
    # `wrong` names the one setting of the broker's configuration that is off.
    #
    off = {
        "token": replace(config, token=new_token()),
        "url": replace(config, url=f"http://127.0.0.1:{unused_tcp_port_factory()}/mcp"),
    }
    server = McpServer(off[wrong])
    with pytest.raises(MajiError) as raised:
        await server.call("get_thing", {"key": "a"}, caller)
    with check:
        assert raised.value.code == "unavailable"
    with check:
        assert raised.value.message.startswith("fake")


async def test_a_call_the_stdio_server_rejects_comes_back_as_its_error(
    config: ServerConfig, caller: CallContext
) -> None:
    """
    GIVEN: A server behind the bridge that rejects a call as a protocol error
     WHEN: the broker makes that call
     THEN: the bridge returns it as an error result, and the call fails with internal,
           carrying the server's message: the server was reached and answered, so it
           isn't an outage
    """
    with pytest.raises(MajiError) as raised:
        await McpServer(config).call("refuse", {}, caller)
    with check:
        assert raised.value.code == "internal"
    with check:
        assert raised.value.message == "refused by the server"


async def test_a_script_reads_through_the_bridge_and_cannot_write(
    stubs_by_name: dict[str, Stub], services: Services
) -> None:
    """
    GIVEN: The broker serving a bridged server's stubs to a principal granted all of
           them
     WHEN: a script reads a thing, then tries the write
     THEN: the read returns the server's answer, and the write is refused, since
           the broker makes no writes until changesets are built
    """
    catalog = Catalog()
    catalog.add("1.0", *stubs_by_name.values())
    services.broker.catalog = catalog
    services.broker.policy = Policy({"reader": Grant(allow=("fake.*",))})
    _, token = await services.start_run("reader")
    client = services.stub_client(token)

    def script() -> dict[str, Any]:
        """Read a thing, try to change it, and return the thing with the write's error."""
        fake = client.namespaces()["fake"]
        thing: dict[str, Any] = fake.get_thing(key="b")
        try:
            fake.set_thing(key="b", value="c")
        except client.StubError as exc:
            thing["write"] = exc.code
        return thing

    result = await asyncio.to_thread(script)
    with check:
        assert result["value"] == "value-of-b"
    with check:
        assert result["write"] == "permission_denied"
