"""
Tests for the gateway, end to end without Docker.

A stand-in for the Docker runner runs each script in this process with the stub
client, using the run's token, exactly as a script in the executor would. Everything
else is real: the broker's sockets, the runner's socket, and the gateway served over
HTTP to an MCP client.
"""

# system imports
import asyncio
import hashlib
import itertools
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

# 3rd party imports
import httpx
import httpx2
import pytest
from faker import Faker
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import Implementation
from pytest_check import check

# project imports
from maji_mcp.broker.store import IDLE_SECONDS
from maji_mcp.common.names import session_name
from maji_mcp.gateway.server import (
    INSTRUCTIONS,
    MAX_CLIENT_TEXT,
    NamedSessions,
    ShortIds,
    TokenAuth,
    build_server,
    client_of,
    format_result,
    note_session,
    session_of,
)
from tests.gateway.factories import ContextFactory, RunResultFactory
from tests.support.gateway import (
    KEEPER_TOKEN,
    LOAD_N,
    READER_TOKEN,
    SAVE_N,
    Connect,
    InProcessRunner,
    printed_by,
)
from tests.support.paths import RUNTIME_REQUIREMENTS
from tests.support.services import FakeClock, Services

type RunScripts = Callable[..., Awaitable[list[str]]]
type Rpc = Callable[..., Awaitable[tuple[httpx.Response, dict[str, Any]]]]


###############################################################################
# The gateway, served over HTTP to MCP clients.
#
@pytest.fixture
def call_tool(connected: Connect) -> Callable[..., Awaitable[str]]:
    """
    A function that calls one of the gateway's tools as the reader, in an MCP session
    of its own, and returns the text of its result.
    """

    async def call(tool: str, **args: Any) -> str:
        async with connected(READER_TOKEN) as session:
            result = await session.call_tool(tool, args)
        assert not result.is_error, result.content
        text: str = result.content[0].text  # type: ignore[union-attr]
        return text

    return call


@pytest.fixture
def in_requests_of_their_own(
    gateway: str, implementation: Implementation
) -> RunScripts:
    """
    A function that runs each script with run_python as a client on protocol 2026-07-28
    does: each request on its own, with no MCP session, naming a session in
    X-Maji-Session if one is given.

    It takes the principal's token, the session to name or None, and the scripts, and
    returns what each script printed, or for a call that failed its error text.
    """

    async def run(token: str, named: str | None, *codes: str) -> list[str]:
        headers = {"Authorization": f"Bearer {token}"}
        if named is not None:
            headers["X-Maji-Session"] = named
        texts = []
        async with (
            httpx2.AsyncClient(headers=headers) as http,
            Client(
                streamable_http_client(gateway, http_client=http),
                mode="2026-07-28",
                client_info=implementation,
            ) as client,
        ):
            for code in codes:
                result = await client.call_tool("run_python", {"code": code})

                # The last line is the run's status; what the script printed is
                # above it.
                #
                text: str = result.content[0].text  # type: ignore[union-attr]
                texts.append(text if result.is_error else text.rpartition("\n")[0])
        return texts

    return run


@pytest.fixture
def in_a_new_session(
    request: pytest.FixtureRequest,
    connected: Connect,
    in_requests_of_their_own: RunScripts,
    faker: Faker,
) -> RunScripts:
    """
    A function that runs scripts with run_python as the keeper, in order, in one new
    session, and returns what each printed.

    The session is of the kind the test's parameter names: `an MCP session`, which the
    gateway issues to a client on an earlier protocol revision, or `a session its app
    names` in X-Maji-Session, as a client on 2026-07-28 has.
    """

    async def in_an_mcp_session(*codes: str) -> list[str]:
        async with connected(KEEPER_TOKEN) as session:
            return [await printed_by(session, code) for code in codes]

    async def in_a_named_session(*codes: str) -> list[str]:
        return await in_requests_of_their_own(KEEPER_TOKEN, faker.md5(), *codes)

    kinds = {
        "an MCP session": in_an_mcp_session,
        "a session its app names": in_a_named_session,
    }
    return kinds[request.param]


@pytest.fixture
async def rpc(gateway: str) -> AsyncIterator[Rpc]:
    """
    A function that posts one JSON-RPC message to the gateway as the keeper, for
    requests the SDK's client wouldn't send.

    It takes the method, its params, and any `headers` to add; a message with no
    params is sent as a notification. It returns the HTTP response and the JSON-RPC
    message in it, sent as JSON or as one server-sent event, or `{}` when it has none.
    """
    ids = itertools.count(1)

    def message_in(response: httpx.Response) -> dict[str, Any]:
        kind = response.headers.get("content-type", "")
        if kind.startswith("text/event-stream"):
            data = [
                line[5:]
                for line in response.text.splitlines()
                if line.startswith("data:")
            ]
            return dict(json.loads(data[-1]))
        if kind.startswith("application/json") and response.content:
            return dict(response.json())
        return {}

    async with httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {KEEPER_TOKEN}",
            "Accept": "application/json, text/event-stream",
        }
    ) as http:

        async def post(
            method: str,
            params: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
        ) -> tuple[httpx.Response, dict[str, Any]]:
            message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                message |= {"id": next(ids), "params": params}
            response = await http.post(gateway, headers=headers, json=message)
            return response, message_in(response)

        yield post


@pytest.mark.parametrize(
    "in_a_new_session",
    ["an MCP session", "a session its app names"],
    indirect=True,
)
async def test_scripts_in_one_session_share_saved_values(
    in_a_new_session: RunScripts,
) -> None:
    """
    GIVEN: An agent whose principal may use the maji stubs, on a client with an MCP
           session, or on one on protocol 2026-07-28, which has none, as Claude Code
           speaks it, naming its session in X-Maji-Session
     WHEN: one script saves a value and a later script in the same session loads it,
           then a script in another session tries to load it
     THEN: the later script gets the value, and the other session's gets not_found
    """
    assert await in_a_new_session(SAVE_N, LOAD_N) == ["", "[1, 2, 3]"]
    assert await in_a_new_session(LOAD_N) == ["not_found"]


async def test_a_client_without_any_session_is_told_why_it_cant_keep_values(
    in_requests_of_their_own: RunScripts,
) -> None:
    """
    GIVEN: A client on protocol 2026-07-28 that names no session
     WHEN: its script saves a value
     THEN: the script runs, and the save fails as unavailable, saying the agent's app
           sent neither an MCP session nor an X-Maji-Session header
    """
    probe = """
try:
    maji.save('n', 1)
except maji.StubError as exc:
    print(exc.code, '|', exc)
"""
    [said] = await in_requests_of_their_own(KEEPER_TOKEN, None, probe)
    code, message = said.split(" | ")
    with check:
        assert code == "unavailable"
    with check:
        assert "neither an MCP session nor an X-Maji-Session header" in message


async def test_a_session_id_the_gateway_doesnt_take_fails_the_call(
    in_requests_of_their_own: RunScripts, fake_runner: InProcessRunner
) -> None:
    """
    GIVEN: A client on protocol 2026-07-28 naming its session with characters the
           gateway doesn't take
     WHEN: it calls run_python
     THEN: the call fails as invalid_argument, saying what an id may hold, and no
           script runs
    """
    [said] = await in_requests_of_their_own(READER_TOKEN, "not a session", "print(1)")
    with check:
        assert said == (
            "Error executing tool run_python: invalid_argument: X-Maji-Session must "
            "be 16 to 128 letters, digits, '-' or '_'"
        )
    with check:
        assert fake_runner.tokens == []


async def test_an_unknown_mcp_session_is_refused_before_any_tool_runs(
    rpc: Rpc, fake_runner: InProcessRunner, faker: Faker
) -> None:
    """
    GIVEN: A request with a valid token, naming an MCP session this gateway never
           issued
     WHEN: it calls run_python
     THEN: the gateway answers 404 and no script runs, so a session id can only reach
           values saved under a session the gateway issued
    """
    response, _ = await rpc(
        "tools/call",
        {"name": "run_python", "arguments": {"code": "print(1)"}},
        headers={"mcp-session-id": faker.md5()},
    )
    with check:
        assert response.status_code == 404
    with check:
        assert fake_runner.tokens == []


@pytest.mark.parametrize("offered", ["2025-06-18", "2025-11-25"])
async def test_a_client_on_an_earlier_protocol_revision_can_run_scripts(
    rpc: Rpc, implementation: Implementation, offered: str
) -> None:
    """
    GIVEN: A client that offers an earlier MCP protocol revision, as clients built on
           the 1.x SDK do
     WHEN: it initializes a session with the gateway and calls run_python
     THEN: the gateway agrees to that revision and sends its instructions, and the
           script runs
    """
    # The gateway agrees to the revision the client offers, and sends its
    # instructions.
    #
    init, answer = await rpc(
        "initialize",
        {
            "protocolVersion": offered,
            "capabilities": {},
            "clientInfo": {
                "name": implementation.name,
                "version": implementation.version,
            },
        },
    )
    with check:
        assert answer["result"]["protocolVersion"] == offered
    with check:
        assert answer["result"]["instructions"] == INSTRUCTIONS

    # A script runs in the MCP session the gateway issued.
    #
    session = {
        "mcp-session-id": init.headers["mcp-session-id"],
        "mcp-protocol-version": offered,
    }
    await rpc("notifications/initialized", headers=session)
    _, answer = await rpc(
        "tools/call",
        {"name": "run_python", "arguments": {"code": "print(6 * 7)"}},
        headers=session,
    )
    assert answer["result"]["isError"] is False
    assert answer["result"]["content"][0]["text"].split("\n")[0] == "42"


async def test_run_python_runs_as_the_tokens_principal(
    call_tool: Callable[..., Awaitable[str]], services: Services
) -> None:
    """
    GIVEN: An agent connected to the gateway with the reader principal's token
     WHEN: it calls run_python with a script that calls a granted stub
     THEN: the stub is called as the reader, and the agent gets the script's output,
           followed by a one-line run status
    """
    text = await call_tool("run_python", code="print(fake.list(limit=2)['args'])")
    output, status = text.rsplit("\n", 1)
    with check:
        assert [call["principal"] for call in services.handled] == ["reader"]
    with check:
        assert output == "{'limit': 2, 'tags': None}"
    with check:
        assert status.startswith("[run ")
    with check:
        assert status.endswith("exit 0, 3 ms; called fake.list]")


async def test_run_python_passes_a_longer_timeout_to_the_runner(
    call_tool: Callable[..., Awaitable[str]], fake_runner: InProcessRunner
) -> None:
    """
    GIVEN: An agent whose script will make many slow calls
     WHEN: it calls run_python with timeout_s, and then without it
     THEN: the run gets the time it asked for, and otherwise 60 seconds
    """
    await call_tool("run_python", code="print(1)", timeout_s=300)
    await call_tool("run_python", code="print(1)")
    assert [limits.timeout_s for limits in fake_runner.limits] == [300.0, 60.0]


async def test_run_python_counts_sentry_events_by_frame(
    sentry_services: tuple[Services, Any], call_tool: Callable[..., Awaitable[str]]
) -> None:
    """
    GIVEN: The broker serving the sentry stubs against a fake Sentry, and an agent
           connected to the gateway
     WHEN: the agent runs a script that fetches an issue's full events and counts
           them by top frame
     THEN: the agent gets back only the count, computed in the script
    """
    code = (
        "from collections import Counter\n"
        "events = sentry.events(issue_id='7', full=True)\n"
        "print(Counter(e['top_frame'] for e in events).most_common(1))"
    )
    text = await call_tool("run_python", code=code)
    assert text.split("\n")[0] == "[('web/tasks.py:88 ingest', 1)]"


async def test_the_api_tools_show_only_the_principals_stubs(
    call_tool: Callable[..., Awaitable[str]],
) -> None:
    """
    GIVEN: An agent whose principal is granted some of the fake namespace
     WHEN: it lists, reads and searches the API through the gateway
     THEN: it sees the granted stubs with Python signatures, and not the others
      AND: the listing ends by pointing to the libraries a script can import, and
           read_api shows a library's notes
    """
    listing = await call_tool("list_api")
    doc = await call_tool("read_api", name="fake.read")
    found = await call_tool("search_api", query="list")
    notes = await call_tool("read_api", name="python.yaml")
    with check:
        assert "fake 1.0" in listing
    with check:
        assert "fake.secret" not in listing
    with check:
        assert listing.splitlines()[-1] == (
            'python: the libraries a script can import; read_api("python")'
        )
    with check:
        assert doc.startswith("fake.read(*, key: str) -> dict")
    with check:
        assert found.startswith("fake.list(")
    with check:
        assert notes.startswith("yaml: PyYAML ")


async def test_an_agent_is_told_why_its_request_was_refused(
    connected: Connect,
) -> None:
    """
    GIVEN: A gateway whose runner mounts nothing, and a broker without a stub of that
           name
     WHEN: an agent asks run_python to mount a path, and read_api for that stub
     THEN: each call fails with the service's code and its reason, so the agent can
           act on it, rather than a bare "Error executing tool"
    """
    async with connected(READER_TOKEN) as session:
        mounting = await session.call_tool(
            "run_python", {"code": "print(1)", "paths": ["/h/src/api"]}
        )
        reading = await session.call_tool("read_api", {"name": "nothing.here"})
    assert mounting.is_error and reading.is_error
    said = [r.content[0].text for r in (mounting, reading)]  # type: ignore[union-attr]
    with check:
        assert said[0] == (
            "Error executing tool run_python: invalid_argument: this runner mounts "
            "no paths"
        )
    with check:
        assert said[1].startswith("Error executing tool read_api: not_found: ")


async def test_a_client_ending_its_session_leaves_no_error_in_the_log(
    call_tool: Callable[..., Awaitable[str]], caplog: pytest.LogCaptureFixture
) -> None:
    """
    GIVEN: A client with an open MCP session, including the event stream the SDK's
           client opens after initializing
     WHEN: it ends the session, as every client does when it exits
     THEN: the gateway logs no error for it
    """
    with caplog.at_level(logging.ERROR):
        await call_tool("list_api")

        # The server tears the session down after the client has gone, so give it a
        # moment before reading the log.
        #
        await asyncio.sleep(0.3)
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer wrong-token", f"Basic {READER_TOKEN}"],
    ids=["no token", "an unknown token", "the right token in the wrong scheme"],
)
async def test_requests_without_a_valid_token_are_refused(
    gateway: str, authorization: str | None
) -> None:
    """
    GIVEN: A request to the gateway with no token, an unknown token, or the right
           token in the wrong scheme
     WHEN: it arrives
     THEN: the gateway answers 401 before any MCP handling
    """
    headers = {"Authorization": authorization} if authorization else {}
    async with httpx.AsyncClient() as client:
        response = await client.post(gateway, headers=headers, json={})
    assert response.status_code == 401


###############################################################################
# The gateway's parts, each on its own.
#
@pytest.mark.parametrize("scope_type", ["websocket", "something-new"])
async def test_a_scope_that_isnt_http_never_reaches_the_app(
    faker: Faker, scope_type: str
) -> None:
    """
    GIVEN: The gateway's token check in front of an app
     WHEN: a connection arrives that isn't HTTP, such as a websocket, carrying a
           principal's token
     THEN: it is refused, and the app behind never sees it
    """
    token = faker.password(length=43, special_chars=False)
    inner = AsyncMock()
    app = TokenAuth(inner, {"reader": hashlib.sha256(token.encode()).hexdigest()})
    scope = {
        "type": scope_type,
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    await app(scope, AsyncMock(), AsyncMock())
    inner.assert_not_awaited()


async def test_the_servers_own_lifespan_passes() -> None:
    """
    GIVEN: The gateway's token check in front of an app
     WHEN: the server starts up, which sends a lifespan scope with no token
     THEN: the app receives it, so its startup and shutdown run
    """
    inner, receive, send = AsyncMock(), AsyncMock(), AsyncMock()
    scope = {"type": "lifespan"}
    await TokenAuth(inner, {})(scope, receive, send)
    inner.assert_awaited_once_with(scope, receive, send)


async def test_run_python_names_every_library_a_script_can_import() -> None:
    """
    GIVEN: The libraries the runtime image installs for scripts, listed in
           images/python-runtime/requirements.in
     WHEN: an agent reads run_python's description
     THEN: it names each one, so an agent knows what it can import without trying
    """
    names = [
        line.strip()
        for line in RUNTIME_REQUIREMENTS.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert names
    tools = await build_server(Path("/runner"), Path("/broker")).list_tools()
    [run_python] = [t for t in tools if t.name == "run_python"]
    assert run_python.description
    for name in names:
        with check:
            assert name in run_python.description


@pytest.mark.parametrize(
    ("changes", "shown"),
    [
        (
            {"stdout": "42\n", "stderr": "DeprecationWarning"},
            "42\n[run {run}: exit 0, {ms} ms]",
        ),
        (
            {"stderr": "Traceback ...\nKeyError: 'x'", "exit_code": 1},
            "Traceback ...\nKeyError: 'x'\n[run {run}: exit 1, {ms} ms]",
        ),
        (
            {"stdout": "42\n", "mounts": {"api": "/h/src/api"}},
            "42\n[run {run}: exit 0, mounted /mnt/api, {ms} ms]",
        ),
        (
            {
                "mounts": {"api": "/home/me/src/api"},
                "called": [
                    {"name": "skills.culprits", "calls": 1, "failed": 0},
                    {"name": "sentry.issues", "calls": 2, "failed": 0},
                    {"name": "sentry.events", "calls": 40, "failed": 3},
                    {"name": "org.update_task", "calls": 1, "failed": 1},
                ],
            },
            "[run {run}: exit 0, mounted /mnt/api, {ms} ms; called skills.culprits, "
            "sentry.issues ×2, sentry.events ×40 (3 failed), org.update_task (failed)]",
        ),
        (
            {
                "called": [
                    {"name": f"fake.s{n}", "calls": 1, "failed": 0} for n in range(14)
                ]
            },
            "[run {run}: exit 0, {ms} ms; called "
            + ", ".join(f"fake.s{n}" for n in range(12))
            + ", and 2 more]",
        ),
    ],
    ids=[
        "a run that succeeded with warnings",
        "a run that failed",
        "a run that mounted a path",
        "a run that called stubs and a skill",
        "a run that called more than are named",
    ],
)
def test_a_run_is_shown_as_its_output_then_a_line_of_status(
    changes: dict[str, Any], shown: str
) -> None:
    """
    GIVEN: A run that exited 0 with warnings on stderr, one that failed, one that
           mounted a path, one that called stubs and a skill, some many times and
           some failing, and one that called more than the result names
     WHEN: the gateway renders each for the agent
     THEN: the successful run shows only its output and status, and the failed run
           also shows its stderr
      AND: the status names where each path is mounted
      AND: the status names what the run called in the order it first called each,
           how many times when more than once, and how many of those failed, so the
           user sees in the agent's transcript which tools a script used
      AND: past twelve, MAX_CALLED_SHOWN, it says how many more there were
    """
    result = RunResultFactory.build(**changes)

    # `{run}` and `{ms}` stand for the run's id, by its first 8 characters, and its
    # duration, which the factory makes up.
    #
    assert format_result(result) == shown.format(
        run=result["run_id"][:8], ms=result["duration_ms"]
    )


def test_the_agent_is_told_the_script_didnt_run_and_what_waits(session: str) -> None:
    """
    GIVEN: A run waiting on a grant
     WHEN: the gateway renders it for the agent
     THEN: it says the script didn't run, which paths and item it waits on, the name
           the queue shows for its session, and to run it again once you've decided
           rather than in a loop
      AND: without a session, it names none
    """
    pending = {"pending": {"items": ["studio-mac-01"], "paths": ["/h/src/api"]}}

    waiting = format_result(pending, session)
    with check:
        assert waiting.startswith("[not run: the user hasn't granted /h/src/api.")
    with check:
        assert (
            "Waiting on studio-mac-01 in their queue, which shows this session as "
            f"{session_name(session)}." in waiting
        )
    with check:
        assert "Run the same request again once they've decided" in waiting

    assert "Waiting on studio-mac-01 in their queue. Run" in format_result(pending)


def test_the_agent_is_told_what_a_mount_left_out() -> None:
    """
    GIVEN: A run whose mounts left files out: one with a few files named like
           secrets, one with more than are named, and one with only places on your
           never-mount list
     WHEN: the gateway renders the run for the agent
     THEN: a line for each mount names the files that read as empty, and how many
           more there are, so an empty read isn't taken for an empty file
      AND: places on your list are counted, never named
    """
    ran = format_result(
        RunResultFactory.build(
            stdout="42\n",
            mounts={"api": "/h/src/api", "web": "/h/src/web", "notes": "/h/notes"},
            left_out={
                "api": {"files": [".env", "deploy/key.pem"], "count": 3},
                "web": {"files": [f"k{n}.pem" for n in range(8)], "count": 8},
                "notes": {"files": [], "count": 2},
            },
        )
    )
    assert ran.splitlines()[1:4] == [
        "[left out of /mnt/api, read as empty: .env, deploy/key.pem, and 1 more]",
        "[left out of /mnt/web, read as empty: k0.pem, k1.pem, k2.pem, k3.pem, "
        "k4.pem, and 3 more]",
        "[left out of /mnt/notes, read as empty: 2 places]",
    ]


@pytest.mark.parametrize(
    ("app", "shown"),
    [
        (
            Implementation(name="claude-code", version="2.1.318"),
            {"name": "claude-code", "version": "2.1.318"},
        ),
        (
            Implementation(name="evil\x1b[31m" + "x" * 200, version=""),
            {"name": ("evil[31m" + "x" * 200)[:MAX_CLIENT_TEXT]},
        ),
        (None, None),
    ],
    ids=["a name and a version", "a long name with a control character", "no name"],
)
def test_an_app_is_shown_as_it_names_itself_cut_to_printable_text(
    app: Implementation | None,
    shown: dict[str, str] | None,
) -> None:
    """
    GIVEN: A client that named itself when it connected, one whose name carries a
           control character and runs long, and one that gave no name
     WHEN: the gateway reads what each said it is
     THEN: the first is its name and version as given; the second is cut to
           printable text of at most MAX_CLIENT_TEXT; the third is None
    """
    assert client_of(ContextFactory.build(app=app)) == shown


def test_a_session_is_logged_once_with_its_app_and_short_ids(
    implementation: Implementation,
    faker: Faker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    GIVEN: A session making its first and second tool calls
     WHEN: the gateway notes it each time
     THEN: it logs one line, the first time, with the session's id and key by their
           first 8 characters, its name, its principal and the app it says it is
      AND: a log line holding a whole session id, as the SDK writes one, is cut to
           its first 8 characters
    """
    session_id, key = faker.md5(), faker.sha256()

    # A session noted twice is logged once, by the first 8 characters of its id and
    # its key.
    #
    ctx = ContextFactory.build(app=implementation, mcp_session=session_id)
    with caplog.at_level(logging.INFO, logger="maji.gateway"):
        note_session(ctx, "claude-code", key)
        note_session(ctx, "claude-code", key)
    with check:
        assert [r.getMessage() for r in caplog.records] == [
            f"session {session_id[:8]} (key {key[:8]}, {session_name(key)}) of "
            f"claude-code, app {implementation.name} {implementation.version}"
        ]

    # A line the SDK logs with the whole session id is cut the same way.
    #
    record = logging.LogRecord(
        "mcp",
        logging.INFO,
        "",
        0,
        "Created new transport with session ID: %s",
        (session_id,),
        None,
    )
    ShortIds().filter(record)
    with check:
        assert record.getMessage() == (
            f"Created new transport with session ID: {session_id[:8]}"
        )


def test_a_named_session_lasts_until_it_goes_an_hour_without_a_script(
    clock: FakeClock, faker: Faker
) -> None:
    """
    GIVEN: The sessions clients name, measured against a clock
     WHEN: a client names the same session again within IDLE_SECONDS, and again after
           going longer than that without a script
     THEN: within the hour it has the same key; after it, a new session begins, with
           a new key, said to have begun for going idle
      AND: the same id under another principal is a different session
    """
    named = faker.md5()
    sessions = NamedSessions(clock=clock)
    first, began = sessions.key("claude-code", named)
    assert began == "new"
    clock.now += IDLE_SECONDS
    assert sessions.key("claude-code", named) == (first, None)
    clock.now += IDLE_SECONDS + 1
    later, began = sessions.key("claude-code", named)
    assert began == "idle" and later != first
    other, began = sessions.key("codex", named)
    assert began == "new" and other not in (first, later)


def test_past_its_limit_the_session_unused_longest_is_forgotten(faker: Faker) -> None:
    """
    GIVEN: The sessions clients name, held to MAX_NAMED_SESSIONS
     WHEN: one more session begins than the limit holds
     THEN: the session unused longest is forgotten, and begins afresh with a new key
           if its client names it again; the others keep theirs
    """
    # Two sessions are enough to show which one is forgotten.
    #
    sessions = NamedSessions(max_sessions=2)
    used, unused, another = faker.md5(), faker.md5(), faker.md5()
    kept, _ = sessions.key("claude-code", used)
    forgotten, _ = sessions.key("claude-code", unused)
    assert sessions.key("claude-code", used) == (kept, None)
    sessions.key("claude-code", another)
    assert sessions.key("claude-code", used) == (kept, None)
    again, began = sessions.key("claude-code", unused)
    assert began == "new" and again != forgotten


def test_an_mcp_session_comes_before_one_the_client_names(
    implementation: Implementation,
    faker: Faker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    GIVEN: Requests with an MCP session, with only X-Maji-Session, with neither, and
           with an id the gateway doesn't take
     WHEN: the gateway finds each one's session
     THEN: the MCP session decides when there is one; otherwise the named session
           does, logged as it begins with its id and key by their first 8 characters,
           its name and the app it says it is; with neither there is none; and an id
           the gateway doesn't take is refused as invalid_argument
    """
    sessions = NamedSessions()
    mcp_id, named_id = faker.md5(), faker.md5()

    # With an MCP session, its id decides the key, whatever session the app names.
    # Without one, the named session does, and has the same key each time.
    #
    both = ContextFactory.build(
        app=implementation, mcp_session=mcp_id, named_session=named_id
    )
    only_named = ContextFactory.build(app=implementation, named_session=named_id)
    with caplog.at_level(logging.INFO, logger="maji.gateway"):
        by_mcp = session_of(both, "claude-code", sessions)
        by_name = session_of(only_named, "claude-code", sessions)
        assert session_of(only_named, "claude-code", sessions) == by_name
    assert by_name and by_name != by_mcp
    with check:
        assert by_mcp == (hashlib.sha256(f"claude-code\0{mcp_id}".encode()).hexdigest())

    # The named session is logged once, as it begins.
    #
    with check:
        assert [r.getMessage() for r in caplog.records] == [
            f"session {named_id[:8]} (key {by_name[:8]}, {session_name(by_name)}) of "
            f"claude-code, app {implementation.name} {implementation.version}, "
            "named by its app"
        ]

    # A request with neither has no session, and an id the gateway doesn't take is
    # refused.
    #
    with check:
        assert session_of(ContextFactory.build(), "claude-code", sessions) is None
    with (
        check,
        pytest.raises(ToolError, match="^invalid_argument: X-Maji-Session must be"),
    ):
        session_of(
            ContextFactory.build(app=None, named_session="x\n" * 10),
            "claude-code",
            sessions,
        )
