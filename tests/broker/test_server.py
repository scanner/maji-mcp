"""
Tests for the broker's decisions on each run and each stub call, over its real sockets.
"""

# system imports
import asyncio
import json
import shutil
import signal
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType
from typing import Any

# 3rd party imports
import pytest
from pytest_check import check

# project imports
from maji_mcp.broker import github, jira, sentry
from maji_mcp.broker.catalog import CallContext, Param, Stub
from maji_mcp.broker.connections import Connections
from maji_mcp.broker.runs import MAX_CALLS_IN_FLIGHT
from maji_mcp.broker.server import build_catalog
from maji_mcp.common.protocol import MajiError, call_unix
from tests.support.services import FakeClock, Services


@pytest.fixture
def call(services: Services) -> Callable[..., Awaitable[Any]]:
    """A function that calls a stub with a run's token, as that run's script does."""

    async def call(token: str, stub: str, **args: Any) -> Any:
        client = services.stub_client(token)
        return await asyncio.to_thread(client.call, stub, **args)

    return call


@pytest.fixture
def refusal(services: Services) -> Callable[..., Awaitable[Any]]:
    """
    A function that makes a call the broker is to refuse, with a run's token, and
    returns the `StubError` that run's script is given.
    """

    async def refusal(token: str, stub: str, **args: Any) -> Any:
        client = services.stub_client(token)
        with pytest.raises(client.StubError) as refused:
            await asyncio.to_thread(client.call, stub, **args)
        return refused.value

    return refusal


###############################################################################
#
# Who a call is from.


async def test_a_call_runs_with_the_principal_of_its_run(
    services: Services, call: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: A run registered by the runner for a principal allowed to read
     WHEN: a script in that run calls a stub, through the stub client
     THEN: the broker runs the stub's handler as that principal, with the checked
           arguments, defaults filled in, and returns its result
    """
    _, token = await services.start_run("reader")
    result = await call(token, "fake.list", limit=2)
    assert result == {"stub": "fake.list", "args": {"limit": 2, "tags": None}}
    assert services.handled == [
        {"stub": "fake.list", "principal": "reader", "limit": 2, "tags": None}
    ]


async def test_a_principal_named_in_the_request_is_ignored(services: Services) -> None:
    """
    GIVEN: A run for a principal that may not call `fake.secret`
     WHEN: its script sends a request that names a more powerful principal
     THEN: the broker decides by the run's own principal, refuses the call, and the
           handler never runs
    """
    _, token = await services.start_run("reader")

    # The stub client never names a principal, so the request is written out, as a
    # script that opened the call socket itself would send it.
    #
    request = {"token": token, "principal": "admin", "stub": "fake.secret", "args": {}}
    with pytest.raises(MajiError) as refused:
        await call_unix(services.call_sock, "call", request)
    assert refused.value.code == "permission_denied"
    assert "reader may not call fake.secret" in refused.value.message
    assert services.handled == []


@pytest.mark.parametrize(
    "token",
    ["", "made-up-token", None],
    ids=["empty", "not the broker's", "missing"],
)
async def test_a_call_without_a_live_token_is_refused(
    services: Services, token: str | None
) -> None:
    """
    GIVEN: A request whose token is missing, or wasn't issued by the broker
     WHEN: it arrives on the call socket
     THEN: the broker refuses it as unauthenticated, before looking at the stub
    """
    params: dict[str, Any] = {"stub": "fake.read", "args": {"key": "a"}}
    if token is not None:
        params["token"] = token
    with pytest.raises(MajiError) as caught:
        await call_unix(services.call_sock, "call", params)
    assert caught.value.code == "unauthenticated"
    assert services.handled == []


async def test_a_finished_runs_token_stops_working(
    services: Services,
    call: Callable[..., Awaitable[Any]],
    refusal: Callable[..., Awaitable[Any]],
) -> None:
    """
    GIVEN: A run that the runner has finished
     WHEN: something presents that run's token again
     THEN: the broker refuses it as unauthenticated
    """
    run_id, token = await services.start_run("reader")
    await call(token, "fake.read", key="a")
    await services.control("finish_run", run_id=run_id, exit_code=0, duration_ms=5)
    error = await refusal(token, "fake.read", key="a")
    assert error.code == "unauthenticated"


@pytest.mark.parametrize(
    ("registration", "code"),
    [
        pytest.param(
            {"principal": "stranger"},
            "permission_denied",
            id="a principal the policy doesn't have",
        ),
        pytest.param(
            {"principal": "keeper", "session": "short"},
            "invalid_argument",
            id="a session too short",
        ),
        pytest.param(
            {"principal": "keeper", "session": "G" * 64},
            "invalid_argument",
            id="a session that isn't hex",
        ),
        pytest.param(
            {"principal": "keeper", "session": 7},
            "invalid_argument",
            id="a session that isn't text",
        ),
    ],
)
async def test_a_registration_the_broker_wont_take_starts_no_run(
    services: Services, registration: dict[str, Any], code: str
) -> None:
    """
    GIVEN: A registration for a principal that isn't in the policy, or one whose
           session isn't a SHA-256 in hex
     WHEN: the runner registers the run
     THEN: the broker refuses it, as `permission_denied` for the principal and as
           `invalid_argument` for the session
      AND: no run starts, so no token is ever issued for it
    """
    with pytest.raises(MajiError) as refused:
        await services.control("register_run", runtime="python", **registration)
    assert refused.value.code == code
    assert len(services.broker.registry) == 0


###############################################################################
#
# What a call may do.

FAKE_READ = {"signature": "fake.read(*, key: str) -> dict"}


@pytest.mark.parametrize(
    ("principal", "stub", "args", "code", "says", "data"),
    [
        pytest.param(
            "reader",
            "fake.secret",
            {},
            "permission_denied",
            "reader may not call fake.secret",
            {},
            id="a stub outside its grants",
        ),
        pytest.param(
            "reader",
            "fake.update",
            {"key": "a"},
            "permission_denied",
            "can't make writes yet",
            {},
            id="a write",
        ),
        pytest.param(
            "admin",
            "fake.raed",
            {"key": "a"},
            "not_found",
            "no stub 'fake.raed'",
            {"available": ["fake.list", "fake.read", "fake.secret", "fake.update"]},
            id="a stub its namespace doesn't have",
        ),
        pytest.param(
            "reader",
            "fake.raed",
            {"key": "a"},
            "not_found",
            "no stub 'fake.raed'",
            {"available": ["fake.list", "fake.read", "fake.update"]},
            id="a stub missing from a namespace it is granted part of",
        ),
        pytest.param(
            "reader",
            "maji.nope",
            {},
            "not_found",
            "no stub 'maji.nope'",
            {"available": []},
            id="a stub missing from a namespace it is granted none of",
        ),
        pytest.param(
            "reader",
            "fake.read",
            {"key": "a", "colour": "red"},
            "invalid_argument",
            "no parameter 'colour'",
            FAKE_READ,
            id="an unknown argument",
        ),
        pytest.param(
            "reader",
            "fake.read",
            {},
            "invalid_argument",
            "needs 'key'",
            FAKE_READ,
            id="a missing argument",
        ),
        pytest.param(
            "reader",
            "fake.read",
            {"key": 7},
            "invalid_argument",
            "'key' must be str",
            FAKE_READ,
            id="a mistyped argument",
        ),
    ],
)
async def test_a_call_the_broker_refuses_never_reaches_the_service(
    services: Services,
    refusal: Callable[..., Awaitable[Any]],
    principal: str,
    stub: str,
    args: dict[str, Any],
    code: str,
    says: str,
    data: dict[str, Any],
) -> None:
    """
    GIVEN: A call the broker won't make: to a stub outside the principal's grants,
           to a granted stub that writes, to a stub its namespace doesn't have, or
           with an unknown, missing or mistyped argument
     WHEN: a run's script makes it
     THEN: the script is given the contract's code for it, and what it needs to put
           it right: who may not call what, that maji-mcp can't make writes yet, the
           stubs of that namespace it may call, or the argument at fault with the
           stub's signature
      AND: a name that is no stub's never tells it of a stub outside its grants
      AND: the service is never called
      AND: the run log records the call with the code it was refused with
    """
    run_id, token = await services.start_run(principal)
    error = await refusal(token, stub, **args)

    with check:
        assert error.code == code
    with check:
        assert says in error.message
    with check:
        assert error.data == data
    with check:
        assert services.handled == []
    with check:
        assert services.broker.runlog.calls(run_id) == [(stub, code)]


async def test_calls_over_the_rate_limit_are_refused(
    services: Services,
    clock: FakeClock,
    call: Callable[..., Awaitable[Any]],
    refusal: Callable[..., Awaitable[Any]],
) -> None:
    """
    GIVEN: A principal allowed 3 calls a minute to a namespace
     WHEN: its runs make a fourth call within the minute, then another after waiting
     THEN: the fourth is refused as rate_limited, saying how long to wait, and the
           call after the wait succeeds
    """
    _, token = await services.start_run("reader")
    for _ in range(3):
        await call(token, "fake.read", key="a")
    error = await refusal(token, "fake.read", key="a")
    assert error.code == "rate_limited"
    assert error.data["retry_after_s"] > 0
    clock.now += 20
    assert await call(token, "fake.read", key="a")


async def test_parallel_calls_run_at_once_up_to_the_runs_limit(
    services: Services, run_script: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: A stub that takes a moment to answer, and a script that calls it for 20
           items with maji.parallel on 16 threads
     WHEN: the script runs
     THEN: the calls overlap, but the broker never works on more than
           MAX_CALLS_IN_FLIGHT of the run's calls at once, and the results come back
           in the items' order
    """
    running = most = 0

    async def slow(args: dict[str, Any], ctx: CallContext) -> int:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.05)
        running -= 1
        return int(args["n"])

    services.broker.catalog.add(
        "1.0",
        Stub("fake.slow", "Answer slowly.", {"n": Param("int", "n")}, "int", slow),
    )

    def script(maji: ModuleType) -> list[int]:
        fake = maji.namespaces()["fake"]
        result: list[int] = maji.parallel(
            lambda n: fake.slow(n=n), range(20), workers=16
        )
        return result

    assert await run_script(script, "keeper") == list(range(20))
    assert 1 < most <= MAX_CALLS_IN_FLIGHT


###############################################################################
#
# What a principal is shown, and what is recorded.


async def test_a_script_sees_only_the_stubs_it_may_call(
    run_script: Callable[..., Awaitable[Any]],
) -> None:
    """
    GIVEN: A run for a principal granted some of a namespace's stubs
     WHEN: its script asks the broker what it may call
     THEN: the list holds exactly the granted stubs, with their signatures
    """

    def script(maji: ModuleType) -> tuple[list[dict[str, Any]], list[str]]:
        return maji.describe(), list(maji.namespaces())

    described, namespaces = await run_script(script, "reader")
    assert [(d["name"], d["signature"]) for d in described] == [
        ("fake.list", "fake.list(*, limit: int = 10, tags: list[str] = None) -> dict"),
        ("fake.read", "fake.read(*, key: str) -> dict"),
        ("fake.update", "fake.update(*, key: str) -> dict"),
    ]
    assert namespaces == ["fake"]


async def test_the_api_documentation_follows_the_grants(services: Services) -> None:
    """
    GIVEN: A principal granted some of a namespace's stubs
     WHEN: the gateway lists, reads and searches the API for it
     THEN: each shows only the granted stubs, with Python signatures, and reading an
           ungranted stub answers not_found
    """
    listing = await services.control("list_api", principal="reader")
    with check:
        assert listing["namespaces"] == {"fake": "1.0"}
    with check:
        assert "fake.secret" not in [s["name"] for s in listing["stubs"]]

    doc = await services.control("read_api", principal="reader", name="fake.list")
    with check:
        assert doc.startswith(
            "fake.list(*, limit: int = 10, tags: list[str] = None) -> dict"
        )

    found = await services.control("search_api", principal="reader", query="change")
    with check:
        assert [s["name"] for s in found] == ["fake.update"]

    with pytest.raises(MajiError) as caught:
        await services.control("read_api", principal="reader", name="fake.secret")
    assert caught.value.code == "not_found"


async def test_the_run_log_records_each_call_and_the_runs_end(
    services: Services,
    call: Callable[..., Awaitable[Any]],
    refusal: Callable[..., Awaitable[Any]],
) -> None:
    """
    GIVEN: A run that makes a successful call and a refused one, then finishes
     WHEN: the run log is read
     THEN: it holds both calls in order with their outcomes, and the run's exit code
           and duration
    """
    run_id, token = await services.start_run("reader")
    await call(token, "fake.read", key="a")
    await refusal(token, "fake.secret")
    await services.control(
        "finish_run", run_id=run_id, exit_code=0, duration_ms=42, output_bytes=7
    )
    assert services.broker.runlog.calls(run_id) == [
        ("fake.read", "ok"),
        ("fake.secret", "permission_denied"),
    ]
    row = services.broker.runlog.db.execute(
        "SELECT principal, exit_code, duration_ms FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert row == ("reader", 0, 42)


def test_the_broker_has_each_services_stubs_before_it_is_connected() -> None:
    """
    GIVEN: A broker that can call Sentry, Jira and GitHub with keys, and holds none
           yet
     WHEN: it builds its catalog
     THEN: each service's stubs are there beside the built-in ones, so the first
           call to one can ask you to connect that service
    """
    connections = Connections(
        [sentry.service("https://sentry.test"), jira.service(), github.service()],
        None,
        None,
    )

    assert sorted(build_catalog(connections).stubs) == [
        "github.commits",
        "github.pull",
        "github.pulls",
        "github.repos",
        "github.search",
        "jira.changelog",
        "jira.count",
        "jira.fields",
        "jira.issue",
        "jira.issues",
        "jira.projects",
        "maji.load",
        "maji.save",
        "maji.saved",
        "maji.whoami",
        "sentry.environments",
        "sentry.events",
        "sentry.issues",
        "sentry.projects",
        "sentry.tag_values",
    ]


###############################################################################
#
# Stopping.


@pytest.mark.parametrize(
    "sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"]
)
async def test_a_signalled_broker_stops_by_itself(
    tmp_path: Path, sig: signal.Signals
) -> None:
    """
    GIVEN: The maji-broker command serving its sockets
     WHEN: it receives SIGTERM, as Docker sends a container it is stopping, or
           SIGINT, as Ctrl-C sends
     THEN: it exits on its own, cleanly, and says it stopped, so Docker never has to
           kill it
    """
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"principals": {"reader": {"allow": ["maji.*"]}}}))

    # macOS limits a Unix socket's path to about 104 bytes, and pytest's tmp_path is
    # longer than that, so the sockets live in a short directory of their own.
    #
    sockets = Path(tempfile.mkdtemp(prefix="maji-", dir="/tmp"))
    (sockets / "c").mkdir()
    (sockets / "k").mkdir()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "maji_mcp.broker.server",
        "--call-dir",
        str(sockets / "c"),
        "--control-dir",
        str(sockets / "k"),
        "--policy",
        str(policy),
        "--run-log",
        str(tmp_path / "runs.db"),
        "--servers",
        str(tmp_path / "no-servers.json"),
        stdout=asyncio.subprocess.PIPE,
    )

    # It is signalled once it says it is serving. It has to end by itself: it is
    # killed only if it is still running when the test gives up.
    #
    said = b""
    try:
        assert proc.stdout is not None
        async with asyncio.timeout(20):
            while b"serving" not in said:
                line = await proc.stdout.readline()
                assert line, "the broker ended before it served"
                said += line
        proc.send_signal(sig)
        async with asyncio.timeout(10):
            rest, _ = await proc.communicate()
        said += rest
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        shutil.rmtree(sockets, ignore_errors=True)

    with check:
        assert proc.returncode == 0
    with check:
        assert said.decode().splitlines()[-1:] == ["broker: stopped"]
