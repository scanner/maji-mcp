"""
The broker: the only component that holds credentials and reaches services.

It runs no agent code. It serves two Unix sockets, each on its own volume, so which
containers mount a volume decides who can reach that socket:

- **The call socket,** shared with every executor. A script's stub calls arrive here,
  each carrying its run's token. The broker finds the run from the token, then checks
  the call against the principal's grants, refuses writes until changesets exist,
  applies the rate limit, checks the arguments, calls the service and logs the call.
- **The control socket,** shared only with the trusted runner and gateway. The runner
  registers and finishes runs here, and the gateway reads the API documentation for a
  principal, and saves skills.

A script calls a saved skill as `skills.<name>(...)`: its code comes from the library
over the call socket, checked like a stub call, and runs in the script's own process
(`broker/skills.py`). Your decisions on promoting skills reach the broker through the
control plane's decision feed (`broker/decisions.py`).

A run that names paths to mount starts only if the control plane's grants cover them
all; otherwise the broker raises one queue item for the rest (`broker/mounts.py`).

Run it with:

    maji-broker --call-dir /run/maji --control-dir /run/maji-control \\
        --policy policy.json --run-log /var/lib/maji/runs.db \\
        --control-plane /run/maji-plane-broker/control.sock
"""

# system imports
import argparse
import asyncio
import builtins
import json
import os
import re
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

# project imports
from maji_mcp.broker import emacs_org, github, jira, mcp_servers, sentry
from maji_mcp.broker.catalog import CallContext, Catalog, Param, Stub
from maji_mcp.broker.connections import Connections
from maji_mcp.broker.credentials import CredentialStore
from maji_mcp.broker.decisions import Applier, DecisionFeed
from maji_mcp.broker.library import Library
from maji_mcp.broker.mounts import (
    MAX_WHY,
    MountGrants,
    checked_client,
    checked_exact,
    checked_paths,
)
from maji_mcp.broker.policy import Policy, RateLimiter
from maji_mcp.broker.runlog import RunLog
from maji_mcp.broker.runs import Run, RunRegistry
from maji_mcp.broker.skills import SkillService
from maji_mcp.broker.store import SessionStore
from maji_mcp.common import runtime_libraries
from maji_mcp.common.protocol import MajiError, UnknownMethod, serve_unix
from maji_mcp.common.stopping import until_stopped

STUB_TIMEOUT_S = 120.0

# The session key the gateway derives for a run: a SHA-256 in hex.
#
SESSION_KEY = re.compile(r"^[0-9a-f]{64}$")

# What a run's report of the libraries it used may hold: names like Python
# identifiers, so a module or an exception class, at most `MAX_REPORTED` of them, and
# counts up to `MAX_ASKED`.
#
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
MAX_REPORTED = 256

# The exception classes a failure is recorded by when no runtime library raised it:
# Python's own, and the stub client's. Any other is recorded as OTHER_ERROR.
#
KNOWN_ERRORS = frozenset(
    {
        name
        for name, value in vars(builtins).items()
        if isinstance(value, type) and issubclass(value, BaseException)
    }
    | {"StubError"}
)
OTHER_ERROR = "other"
MAX_ASKED = 1000


###############################################################################
#
class Broker:
    """
    Handles both sockets' requests.

    Args:
        catalog: the stubs it serves
        policy: who may call what
        runlog: where runs and calls are recorded
        registry: the live runs and their tokens
        limiter: the rate limits
        mounts: decides which paths a run may mount; without one, none
        skills: the skill library; without one, there are no skills
        feed: your decisions, read before anything that depends on them
    """

    def __init__(
        self,
        catalog: Catalog,
        policy: Policy,
        runlog: RunLog,
        registry: RunRegistry | None = None,
        limiter: RateLimiter | None = None,
        mounts: MountGrants | None = None,
        skills: SkillService | None = None,
        feed: DecisionFeed | None = None,
    ) -> None:
        self.catalog = catalog
        self.policy = policy
        self.runlog = runlog
        self.registry = registry or RunRegistry()
        self.limiter = limiter or RateLimiter()
        self.mounts = mounts or MountGrants(None)
        self.skills = skills
        self.feed = feed

    async def _fresh(self) -> None:
        """Apply the decisions made since the feed was last read, if there is one."""
        if self.feed is not None:
            await self.feed.fresh()

    ###########################################################################
    #
    async def handle_call(self, method: str, params: dict[str, Any]) -> Any:
        """
        Serve the call socket.

        The principal comes from the run the token belongs to. Any principal the
        request itself names is ignored.
        """
        run = self.registry.lookup(params.get("token"))
        if method == "describe":
            return [
                {"name": s.name, "signature": s.signature(), "summary": s.summary}
                for s in self._visible(run.principal)
            ]
        if method == "call":
            name = params.get("stub")
            if not isinstance(name, str):
                raise MajiError("invalid_argument", "a call names a stub")
            args = params.get("args") or {}
            if not isinstance(args, dict):
                raise MajiError("invalid_argument", "a call's args are an object")
            run.calls += 1
            async with run.slots:
                return await self._call(run, name, args)
        if method == "load_skill":
            return await self._load_skill(run, params.get("name"))
        if method == "used":
            # The executor's own report as a script ends (`executor/run.py`). The
            # script could send it too, so it is taken as a count, and once a run.
            #
            if not run.reported:
                run.reported = True
                self.runlog.used(run.run_id, *checked_use(params))
            return {}
        raise UnknownMethod(method)

    async def _load_skill(self, run: Run, name: object) -> dict[str, Any]:
        """
        Serve a skill's code to a run, logged as a call to `skills.<name>`, and
        counted against the principal's rate limit for the `skills` namespace.
        """
        started = time.monotonic()
        outcome, size = "ok", 0
        logged = f"skills.{name}" if isinstance(name, str) else "skills.?"
        try:
            if self.skills is None:
                raise MajiError("not_found", "this broker has no skill library")
            self.limiter.take(
                run.principal, "skills", self.policy.per_minute(run.principal)
            )
            await self._fresh()
            found = self.skills.load(run, name)
            size = len(found["source"])
            return found
        except MajiError as exc:
            outcome = exc.code
            raise
        finally:
            self.runlog.call(
                run.run_id,
                logged[:80],
                outcome,
                int((time.monotonic() - started) * 1000),
                size,
            )

    async def _call(self, run: Run, name: str, args: dict[str, Any]) -> Any:
        run_id, principal = run.run_id, run.principal
        started = time.monotonic()
        outcome, size = "ok", 0
        try:
            stub = self.catalog.get(name, lambda n: self.policy.allows(principal, n))
            if not self.policy.allows(principal, name):
                raise MajiError("permission_denied", f"{principal} may not call {name}")
            if stub.writes:
                raise MajiError(
                    "permission_denied",
                    f"{name} writes, and maji-mcp can't make writes yet: they "
                    "will wait for the user's approval in the queue, which isn't "
                    "built for writes",
                )
            self.limiter.take(
                principal, stub.namespace, self.policy.per_minute(principal)
            )
            bound = stub.bind(args)
            try:
                result = await asyncio.wait_for(
                    stub.handler(bound, CallContext(principal, run_id, run.session)),
                    STUB_TIMEOUT_S,
                )
            except TimeoutError as exc:
                raise MajiError("unavailable", f"{name} timed out") from exc
            size = len(json.dumps(result, default=str))
            return result
        except MajiError as exc:
            outcome = exc.code
            raise
        except Exception:
            outcome = "internal"
            raise
        finally:
            self.runlog.call(
                run_id, name, outcome, int((time.monotonic() - started) * 1000), size
            )

    def _visible(self, principal: str) -> list[Stub]:
        return self.catalog.visible(lambda name: self.policy.allows(principal, name))

    ###########################################################################
    #
    async def handle_control(self, method: str, params: dict[str, Any]) -> Any:
        """Serve the control socket: run registration, and API documentation."""
        if method == "register_run":
            principal = _text(params, "principal")
            if not self.policy.knows(principal):
                raise MajiError("permission_denied", f"unknown principal {principal}")
            runtime = _text(params, "runtime")
            session = params.get("session")
            if session is not None and not (
                isinstance(session, str) and SESSION_KEY.match(session)
            ):
                raise MajiError("invalid_argument", "session is a SHA-256 in hex")
            mounts = checked_paths(params.get("mounts"))
            why = params.get("why")
            if why is not None and (not isinstance(why, str) or len(why) > MAX_WHY):
                raise MajiError(
                    "invalid_argument", f"why is a string of at most {MAX_WHY}"
                )
            # Paths to look through, or a run that waits on you, end here, before a
            # run is registered or a token made.
            #
            waits = await self.mounts.check(
                principal,
                session,
                mounts,
                why,
                params.get("findings"),
                checked_client(params.get("client")),
                checked_exact(params.get("exact"), mounts),
            )
            if waits is not None:
                return waits
            run, token = self.registry.register(principal, runtime, session)
            self.runlog.start(
                run.run_id, principal, runtime, params.get("script_sha256")
            )
            return {"run_id": run.run_id, "token": token, "mounts": mounts}
        if method == "finish_run":
            run_id = _text(params, "run_id")
            self.registry.finish(run_id)
            self.runlog.finish(
                run_id,
                int(params.get("duration_ms") or 0),
                params.get("exit_code"),
                bool(params.get("timed_out")),
                int(params.get("output_bytes") or 0),
            )
            return {"finished": run_id, "called": self.runlog.called(run_id)}
        if method == "list_api":
            principal = _text(params, "principal")
            stubs = [
                {"name": s.name, "summary": s.summary} for s in self._visible(principal)
            ]
            namespaces = {
                ns: self.catalog.versions[ns]
                for ns in sorted({s.namespace for s in self._visible(principal)})
            }
            if self.skills is not None:
                await self._fresh()
                skills = self.skills.listed(principal)
                if skills:
                    namespaces["skills"] = "library"
                    stubs += skills
            return {"namespaces": namespaces, "stubs": stubs}
        if method == "read_api":
            principal = _text(params, "principal")
            name = _text(params, "name")
            if name == "python" or name.startswith("python."):
                return self._read_library(principal, name)
            if self.skills is not None and (
                name == "skills" or name.startswith("skills.")
            ):
                await self._fresh()
                doc = self.skills.read(principal, name)
                if doc is None:
                    raise MajiError("not_found", f"no skill {name!r} for {principal}")
                return doc
            matches = [
                s
                for s in self._visible(principal)
                if s.name == name or s.namespace == name
            ]
            if not matches:
                raise MajiError(
                    "not_found", f"no stub or namespace {name!r} for {principal}"
                )
            return "\n\n".join(s.doc() for s in matches)
        if method == "search_api":
            principal = _text(params, "principal")
            query = _text(params, "query")
            allowed = lambda n: self.policy.allows(principal, n)  # noqa: E731
            found = [
                {"name": s.name, "signature": s.signature(), "summary": s.summary}
                for s in self.catalog.search(query, allowed)
            ]
            if self.skills is not None:
                await self._fresh()
                found = self.skills.search(principal, query) + found
            return found + runtime_libraries.search(query)
        if method == "save_skill":
            if self.skills is None:
                raise MajiError("unavailable", "this broker has no skill library")
            principal = _text(params, "principal")
            if not self.policy.knows(principal):
                raise MajiError("permission_denied", f"unknown principal {principal}")
            await self._fresh()
            return self.skills.keep({**params, "session": _session(params)})
        if method == "promote_skill":
            if self.skills is None:
                raise MajiError("unavailable", "this broker has no skill library")
            principal = _text(params, "principal")
            if not self.policy.knows(principal):
                raise MajiError("permission_denied", f"unknown principal {principal}")
            await self._fresh()
            return await self.skills.promote({**params, "session": _session(params)})
        raise UnknownMethod(method)

    def _read_library(self, principal: str, name: str) -> str:
        """
        The libraries a script can import, for `read_api("python")`, or one's notes,
        for `read_api("python.<name>")`. Each read is recorded in the run log.
        """
        if name == "python":
            doc = runtime_libraries.listing()
        else:
            library = runtime_libraries.BY_NAME.get(name.removeprefix("python."))
            if library is None:
                raise MajiError(
                    "not_found",
                    f'no library {name!r}: read_api("python") lists them',
                )
            doc = library.described()
        self.runlog.looked_up(principal, name)
        return doc


def checked_use(
    params: dict[str, Any],
) -> tuple[set[str], dict[str, int], str | None, str | None]:
    """
    Read a run's report of the libraries it used, keeping only what names a listed
    library (`maji_mcp.common.runtime_libraries`).

    A module counts as imported only by the library's own name, since importing one
    library can import another's dependencies; `help()` and a failure count against
    the library that owns the module. Anything malformed is left out.

    Returns:
        the libraries imported, the `help()` calls on each, the class of the
        exception the run ended with, and the library it came from
    """
    modules = params.get("imported")
    modules = modules[:MAX_REPORTED] if isinstance(modules, list) else []
    imported = {
        m for m in modules if isinstance(m, str) and m in runtime_libraries.BY_NAME
    }

    asked: dict[str, int] = {}
    counts = params.get("asked")
    counts = list(counts.items())[:MAX_REPORTED] if isinstance(counts, dict) else []
    for module, count in counts:
        owner = runtime_libraries.OWNER.get(module)
        if owner and isinstance(count, int) and count > 0:
            asked[owner] = min(asked.get(owner, 0) + count, MAX_ASKED)

    failed = params.get("failed")
    if not isinstance(failed, dict):
        return imported, asked, None, None
    error, inside = failed.get("error"), failed.get("in")
    if not (isinstance(error, str) and IDENTIFIER.fullmatch(error)):
        return imported, asked, None, None
    raised_in = runtime_libraries.OWNER.get(inside) if isinstance(inside, str) else None

    # A class the script defined itself can carry a project's own names, so only
    # Python's classes, the stub client's, and those a runtime library raised are
    # kept by name.
    #
    if raised_in is None and error not in KNOWN_ERRORS:
        error = OTHER_ERROR
    return imported, asked, error, raised_in


def _session(params: dict[str, Any]) -> str | None:
    """The session key the gateway sent, if any."""
    session = params.get("session")
    if session is not None and not (
        isinstance(session, str) and SESSION_KEY.match(session)
    ):
        raise MajiError("invalid_argument", "session is a SHA-256 in hex")
    return session


def _text(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise MajiError("invalid_argument", f"{key} is required")
    return value


###############################################################################
#
async def _whoami(args: dict[str, Any], ctx: CallContext) -> dict[str, str]:
    return {"principal": ctx.principal, "run_id": ctx.run_id}


def builtin_stubs(store: SessionStore) -> list[Stub]:
    """Return the `maji` stubs: who the run is, and the session's saved values."""

    async def save(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        return store.save(ctx.session, args["name"], args["value"])

    async def load(args: dict[str, Any], ctx: CallContext) -> Any:
        return store.load(ctx.session, args["name"])

    async def saved(args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        return store.saved(ctx.session)

    kept = (
        "Values belong to this session, which can outlast one task: a name "
        "saved for an earlier task is still there, so check saved_at, or choose "
        "names for the task at hand. A session unused for an hour is dropped."
    )
    return [
        Stub(
            "maji.whoami",
            "The principal this run acts as, and its run id.",
            {},
            "dict: principal and run_id",
            _whoami,
        ),
        Stub(
            "maji.save",
            "Keep a value for this session's later scripts, replacing any under "
            "the same name.",
            {
                "name": Param("str", "1 to 64 letters, digits, dots, dashes or _"),
                "value": Param("Any", "anything JSON can hold, up to 8 MB"),
            },
            "dict: name, and bytes, the value's size as JSON",
            save,
            notes=kept + " A session holds up to 32 MB.",
        ),
        Stub(
            "maji.load",
            "Return a value an earlier script in this session saved.",
            {"name": Param("str", "the name it was saved under")},
            "Any: the value, as JSON gave it back (tuples become lists)",
            load,
            notes="Raises not_found, with the saved names, for an unknown name.",
        ),
        Stub(
            "maji.saved",
            "List this session's saved values.",
            {},
            "list[dict]: each with name, bytes and saved_at (UTC, ISO 8601)",
            saved,
            notes=kept,
        ),
    ]


def build_catalog(
    connections: Connections, store: SessionStore | None = None
) -> Catalog:
    """
    Return the catalog of the built-in stubs, and the stubs of every service the broker
    can call with a key.

    A service's stubs are there whether or not you have connected it: the first call
    without a key asks you to connect it (`connections.py`).
    """
    catalog = Catalog()
    catalog.add("1.0", *builtin_stubs(store or SessionStore()))
    if "sentry" in connections.services:
        client = sentry.SentryClient(
            connections.services["sentry"].url, connections.key_for("sentry")
        )
        catalog.add(sentry.VERSION, *sentry.stubs(client))
    if "jira" in connections.services:
        jira_client = jira.JiraClient(
            connections.key_for("jira"), connections.services["jira"].url
        )
        catalog.add(jira.VERSION, *jira.stubs(jira_client))
    if "github" in connections.services:
        github_client = github.GitHubClient(
            connections.services["github"].url, connections.key_for("github")
        )
        catalog.add(github.VERSION, *github.stubs(github_client))
    return catalog


async def add_mcp_servers(
    catalog: Catalog, configs: list[mcp_servers.ServerConfig]
) -> None:
    """
    Add each MCP server's configured tools to the catalog.

    A server that can't be reached at start is reported and left out, so one server
    being down doesn't stop the broker. Reconnecting later is still to come
    (`docs/design/planned/server-contract.md` B6).
    """
    for config in configs:
        server = mcp_servers.McpServer(config)
        try:
            found = await mcp_servers.stubs(server)
        except Exception as exc:  # any failure leaves that server out
            print(
                f"broker: {config.namespace} unavailable: {type(exc).__name__}",
                flush=True,
            )
            continue
        if config.adapter == "emacs-org":
            found += emacs_org.stubs(server)
        catalog.add(config.version, *found)
        print(f"broker: {config.namespace}: {len(found)} stubs", flush=True)


async def serve(
    broker: Broker, call_dir: Path, control_dir: Path
) -> tuple[asyncio.Server, asyncio.Server]:
    """
    Start both sockets.

    The call socket is world-writable because executors run as a different user from
    the broker: what protects it is that only executors mount its volume, and every
    request needs a live run's token. The control socket's volume is mounted only by
    the runner and the gateway.
    """
    call = await serve_unix(call_dir / "call.sock", broker.handle_call, mode=0o666)
    control = await serve_unix(
        control_dir / "control.sock", broker.handle_control, mode=0o660
    )
    return call, control


def main() -> None:
    parser = argparse.ArgumentParser(description="the maji-mcp broker")
    parser.add_argument("--call-dir", type=Path, default=Path("/run/maji"))
    parser.add_argument("--control-dir", type=Path, default=Path("/run/maji-control"))
    parser.add_argument("--policy", type=Path, default=Path("/etc/maji/policy.json"))
    parser.add_argument("--run-log", type=Path, default=Path("/var/lib/maji/runs.db"))
    parser.add_argument(
        "--servers",
        type=Path,
        default=Path("/etc/maji/servers.json"),
        help="MCP servers behind the broker, and the tools each exposes",
    )
    parser.add_argument(
        "--control-plane",
        type=Path,
        default=None,
        help="the control plane's socket for the broker, which grants mounts",
    )
    parser.add_argument(
        "--library",
        type=Path,
        default=None,
        help="the skill library's folder, on a volume only the broker mounts",
    )
    parser.add_argument(
        "--publisher",
        default=os.environ.get("MAJI_PUBLISHER", "me"),
        help="the namespace this instance's skills are saved under",
    )
    parser.add_argument(
        "--decisions",
        type=Path,
        default=Path("/var/lib/maji/decisions.position"),
        help="where the position in the control plane's decision feed is kept",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=None,
        help="the keys you connected services with, on a volume only the broker "
        "mounts; without it, no service can be connected",
    )
    parser.add_argument(
        "--sentry-url",
        default=os.environ.get("SENTRY_URL", "https://sentry.io"),
        help="the Sentry host, the only place its key is sent",
    )
    parser.add_argument(
        "--github-url",
        default=os.environ.get("GITHUB_API_URL", github.API_URL),
        help="GitHub's API, the only place its key is sent: https://api.github.com, "
        "or a GitHub Enterprise Server's https://<host>/api/v3",
    )
    args = parser.parse_args()
    os.umask(0o007)

    async def run() -> None:
        connections = Connections(
            [
                sentry.service(args.sentry_url),
                jira.service(),
                github.service(args.github_url),
            ],
            CredentialStore(args.credentials) if args.credentials else None,
            args.control_plane,
        )
        catalog = build_catalog(connections)
        if args.servers.exists():
            await add_mcp_servers(catalog, mcp_servers.load_config(args.servers))
        policy, runlog = Policy.from_file(args.policy), RunLog(args.run_log)
        skills = feed = None
        appliers: list[Applier] = [connections.apply]
        if args.library is not None:
            library = Library(args.library, args.publisher)
            skills = SkillService(library, catalog, policy, runlog, args.control_plane)
            appliers.append(skills.apply)
        if args.control_plane is not None:
            feed = DecisionFeed(args.control_plane, args.decisions, appliers)
        broker = Broker(
            catalog,
            policy,
            runlog,
            mounts=MountGrants(args.control_plane),
            skills=skills,
            feed=feed,
        )
        await serve(broker, args.call_dir, args.control_dir)
        print(
            f"broker: {len(broker.catalog.stubs)} stubs"
            + (f", {len(skills.library.names())} skills" if skills else "")
            + ", serving",
            flush=True,
        )
        forever: list[Coroutine[Any, Any, object]] = [asyncio.Event().wait()]
        if feed is not None:
            forever.append(feed.run())

            # The web app lists the services the broker can be connected to, and the
            # control plane builds their Connect items, from this report, which waits
            # for the control plane if it isn't up yet.
            #
            forever.append(connections.run())
        await asyncio.gather(*forever)

    asyncio.run(until_stopped(run()))
    print("broker: stopped", flush=True)


if __name__ == "__main__":
    main()
