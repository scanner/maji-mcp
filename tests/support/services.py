"""
Stand-ins the tests drive a broker with: a clock moved by hand, a running broker's
handles, stubs for a pretend service, and a pretend Sentry.
"""

# system imports
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

# 3rd party imports
import httpx

# project imports
from maji_mcp.broker.catalog import CallContext, Param, Stub
from maji_mcp.broker.connections import KeyFor
from maji_mcp.broker.server import Broker
from maji_mcp.common.protocol import call_unix
from tests.support.paths import MAJI_MODULE


def fixed_key(key: str, **settings: str) -> KeyFor:
    """What a service's stubs are given for a key that is always connected."""

    async def key_for(ctx: CallContext) -> tuple[str, dict[str, str]]:
        return key, settings

    return key_for


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Services:
    """
    A running broker, with what a test needs to drive it.

    Attributes:
        broker: the broker itself, for a test to read or change what it holds
        call_sock: the socket scripts call stubs on
        control_sock: the socket the runner registers and finishes runs on
        handled: every call that reached a handler of the pretend service's stubs
    """

    _clients = 0

    def __init__(self, broker: Broker, call_dir: Path, control_dir: Path) -> None:
        self.broker = broker
        self.call_sock = call_dir / "call.sock"
        self.control_sock = control_dir / "control.sock"
        self.handled: list[dict[str, Any]] = []

    async def control(self, method: str, **params: Any) -> Any:
        """Make one call on the broker's control socket, as the runner does."""
        return await call_unix(self.control_sock, method, params)

    async def start_run(
        self, principal: str, session: str | None = None
    ) -> tuple[str, str]:
        """Register a run for a principal, and return the run's id and its token."""
        params: dict[str, Any] = {"principal": principal, "runtime": "python"}
        if session:
            params["session"] = session
        reply = await self.control("register_run", **params)
        return reply["run_id"], reply["token"]

    def stub_client(self, token: str) -> ModuleType:
        """
        Load a fresh copy of the executor's stub client, pointed at this broker.

        The client is a standalone file, copied into the runtime image as `maji`, so it
        is loaded from its path rather than imported from the package. In the image,
        `import maji` registers it in `sys.modules`, where a skill finds its `maji`;
        here each copy is registered under a name of its own, so two runs' clients
        stay apart.
        """
        Services._clients += 1
        name = f"maji_{Services._clients}"
        spec = importlib.util.spec_from_file_location(name, MAJI_MODULE)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        setattr(module, "SOCKET", str(self.call_sock))  # noqa: B010 - a module global
        setattr(module, "TOKEN", token)  # noqa: B010 - a module global
        return module


def fake_stubs(handled: list[dict[str, Any]]) -> list[Stub]:
    """Stubs for a pretend service, recording every call that reaches a handler."""

    def recorder(name: str) -> Callable[[dict[str, Any], CallContext], Any]:
        async def handler(args: dict[str, Any], ctx: CallContext) -> Any:
            handled.append({"stub": name, "principal": ctx.principal, **args})
            return {"stub": name, "args": args}

        return handler

    return [
        Stub(
            "fake.read",
            "Read a thing.",
            {"key": Param("str", "which")},
            "dict",
            recorder("fake.read"),
        ),
        Stub(
            "fake.list",
            "List things.",
            {
                "limit": Param("int", "how many", 10),
                "tags": Param("list[str]", "tags", None),
            },
            "dict",
            recorder("fake.list"),
        ),
        Stub(
            "fake.secret", "Read a secret thing.", {}, "dict", recorder("fake.secret")
        ),
        Stub(
            "fake.update",
            "Change a thing.",
            {"key": Param("str", "which")},
            "dict",
            recorder("fake.update"),
            writes=True,
        ),
    ]


###############################################################################
#
SENTRY_TOKEN = "sntrys_fixture-secret-token"
ISSUES = [
    {
        "id": str(n),
        "culprit": f"web.tasks.job_{n % 3}",
        "count": str(n),
        "title": "boom",
    }
    for n in range(1, 151)
]


class FakeSentry:
    """Answers the Sentry API from fixtures, and records each request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {SENTRY_TOKEN}"
        path = request.url.path
        if path == "/api/0/organizations/acme/projects/":
            return httpx.Response(
                200,
                json=[{"slug": "api", "id": "42", "name": "API", "platform": "python"}],
            )
        if path == "/api/0/organizations/acme/issues/":
            # Two pages of 100 and 50, linked the way Sentry links them.
            #
            cursor = request.url.params.get("cursor")
            page = ISSUES[100:] if cursor else ISSUES[:100]
            headers = {}
            if not cursor:
                headers["Link"] = (
                    "<https://sentry.test/api/0/organizations/acme/issues/?cursor=p2>;"
                    ' rel="next"; results="true"'
                )
            return httpx.Response(200, json=page, headers=headers)
        if path == "/api/0/organizations/acme/issues/7/events/":
            frames = [
                {
                    "filename": "lib/http.py",
                    "lineNo": 1,
                    "function": "send",
                    "inApp": False,
                },
                {
                    "filename": "web/tasks.py",
                    "lineNo": 88,
                    "function": "ingest",
                    "inApp": True,
                },
            ]
            event = {
                "id": "e1",
                "entries": [
                    {
                        "type": "exception",
                        "data": {"values": [{"stacktrace": {"frames": frames}}]},
                    }
                ],
            }
            return httpx.Response(200, json=[event])
        if path == "/api/0/projects/acme/missing/environments/":
            return httpx.Response(404, json={"detail": "not found"})
        if path == "/api/0/organizations/acme/issues/9/tags/environment/":
            return httpx.Response(500)
        return httpx.Response(404)
