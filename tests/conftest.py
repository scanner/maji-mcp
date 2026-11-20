"""
Fixtures shared across the test packages: made-up values that repeat, a clock moved
by hand, session keys, a broker serving real Unix sockets, a runner and a gateway in
front of the broker, and a control plane and a skill library beside it.

A fixture used by one package alone is in that package's own `conftest.py`. A free
port comes from pytest-asyncio's `unused_tcp_port` and `unused_tcp_port_factory`.
"""

# system imports
import asyncio
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

# 3rd party imports
import factory.random
import httpx
import pytest
from faker import Faker

# project imports
from maji_mcp.broker import sentry
from maji_mcp.broker.catalog import Catalog
from maji_mcp.broker.decisions import DecisionFeed
from maji_mcp.broker.library import Library
from maji_mcp.broker.policy import Grant, Policy, RateLimiter
from maji_mcp.broker.runlog import RunLog
from maji_mcp.broker.server import Broker, builtin_stubs, serve
from maji_mcp.broker.skills import SkillService
from maji_mcp.broker.store import SessionStore
from maji_mcp.common.protocol import serve_unix
from maji_mcp.control import internal
from maji_mcp.control.store import ControlStore
from maji_mcp.runner.server import RunnerService
from tests.support.control import INSTANCE, Plane, serving_sockets
from tests.support.gateway import InProcessRunner, serving_gateway
from tests.support.services import (
    SENTRY_TOKEN,
    FakeClock,
    FakeSentry,
    Services,
    fake_stubs,
    fixed_key,
)
from tests.support.skills import Skills


@pytest.fixture(autouse=True)
def seeded_factories() -> None:
    """
    factory_boy's generator, seeded before each test as the `faker` fixture seeds its
    own, so the values a factory makes up are the same from one run to the next.
    """
    factory.random.reseed_random(0)


@pytest.fixture
def clock() -> FakeClock:
    """A clock that moves only when a test moves it."""
    return FakeClock()


@pytest.fixture
def session(faker: Faker) -> str:
    """A session's key, as the gateway derives one: a SHA-256 in hex."""
    return faker.sha256()


@pytest.fixture
def other_session(faker: Faker) -> str:
    """The key of a second session."""
    return faker.sha256()


@pytest.fixture
async def services(clock: FakeClock) -> AsyncIterator[Services]:
    """
    A broker serving its two sockets, with a pretend service's stubs and three
    principals: `reader`, narrow and rate-limited to 3 calls a minute; `admin`, with
    every stub of the pretend service; and `keeper`, with the maji stubs and skills.
    """
    # macOS limits a Unix socket's path to about 104 bytes, and pytest's tmp_path is
    # longer than that, so the sockets live in a short directory of their own.
    #
    root = Path(tempfile.mkdtemp(prefix="maji-", dir="/tmp"))
    call_dir, control_dir = root / "c", root / "k"
    call_dir.mkdir()
    control_dir.mkdir()

    catalog = Catalog()
    handled: list[dict[str, Any]] = []
    catalog.add("1.0", *builtin_stubs(SessionStore(clock)))
    catalog.add("1.0", *fake_stubs(handled))
    policy = Policy(
        {
            "reader": Grant(
                allow=("fake.read", "fake.list", "fake.update"), per_minute=3
            ),
            "admin": Grant(allow=("fake.*",), per_minute=100),
            "keeper": Grant(allow=("maji.*", "fake.*", "skills.*"), per_minute=1000),
        }
    )
    broker = Broker(catalog, policy, RunLog(":memory:"), limiter=RateLimiter(clock))
    call, control = await serve(broker, call_dir, control_dir)
    services = Services(broker, call_dir, control_dir)
    services.handled = handled
    try:
        yield services
    finally:
        # Since Python 3.12, `wait_closed` waits for every connection to close, and a
        # stub client keeps its connection open for the whole run, so the servers
        # close their clients first.
        #
        for server in (call, control):
            server.close()
            server.close_clients()
        await asyncio.gather(call.wait_closed(), control.wait_closed())
        services.broker.runlog.db.close()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def fake_runner(services: Services) -> InProcessRunner:
    """
    The stand-in for Docker: it runs each script in this process with the stub client
    a script in an executor has, and records each run it is given.
    """
    return InProcessRunner(services)


@pytest.fixture
def runner_service(services: Services, fake_runner: InProcessRunner) -> RunnerService:
    """The runner service, with `fake_runner` in place of Docker."""
    return RunnerService(fake_runner, services.control_sock)  # type: ignore[arg-type]


@pytest.fixture
async def runner_socket(
    services: Services, runner_service: RunnerService
) -> AsyncIterator[Path]:
    """The socket `runner_service` is serving on."""
    sock = services.call_sock.parent.parent / "runner.sock"
    server = await serve_unix(sock, runner_service.handle)
    yield sock
    server.close()
    server.close_clients()
    await server.wait_closed()


@pytest.fixture
async def gateway(
    services: Services, runner_socket: Path, unused_tcp_port: int
) -> AsyncIterator[str]:
    """The gateway served over HTTP, as the URL of its MCP endpoint."""
    async with serving_gateway(services, runner_socket, unused_tcp_port) as url:
        yield url


@pytest.fixture
async def plane() -> AsyncIterator[Plane]:
    """
    A control plane for the broker to ask: its database, in memory, and its two
    internal sockets. Its REST API isn't served; a test reads the queue and records
    your decisions in the database.
    """
    store = ControlStore(":memory:", INSTANCE)
    try:
        async with serving_sockets(store) as root:
            yield Plane(
                store,
                root / "broker" / internal.SOCKET,
                root / "gateway" / internal.SOCKET,
            )
    finally:
        store.db.close()


@pytest.fixture
def skills(services: Services, plane: Plane, tmp_path: Path, session: str) -> Skills:
    """
    The broker with a skill library, the control plane to put promotions to, and the
    feed that brings your decisions back. Two more principals may call skills,
    `helper` and `third`, beside `keeper`; `reader` still may not.
    """
    broker = services.broker
    broker.policy.grants["helper"] = Grant(allow=("fake.*", "skills.*"))
    broker.policy.grants["third"] = Grant(allow=("fake.*", "skills.*"))
    library = Library(tmp_path / "library", "me")
    broker.skills = SkillService(
        library, broker.catalog, broker.policy, broker.runlog, plane.broker_socket
    )
    feed = DecisionFeed(
        plane.broker_socket, tmp_path / "position", [broker.skills.apply]
    )
    broker.feed = feed
    return Skills(services, plane.store, library, feed, session)


@pytest.fixture
async def sentry_services(services: Services) -> tuple[Services, FakeSentry]:
    """The broker with the `sentry` stubs alone, answered by a pretend Sentry."""
    fake = FakeSentry()
    client = sentry.SentryClient(
        "https://sentry.test",
        fixed_key(SENTRY_TOKEN, org="acme"),
        transport=httpx.MockTransport(fake),
    )
    catalog = Catalog()
    catalog.add(sentry.VERSION, *sentry.stubs(client))
    services.broker.catalog = catalog
    services.broker.policy = Policy({"reader": Grant(allow=("sentry.*",))})
    services.broker.runlog.db.close()
    services.broker.runlog = RunLog(":memory:")
    return services, fake
