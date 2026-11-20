"""
The gateway: the MCP server agents connect to.

It serves MCP over streamable HTTP, published on `127.0.0.1` only. Each principal has
its own bearer token, and the token decides the principal; nothing an agent sends can
change it. The tools:

- `run_python(code)`: run a script in the sandbox, with the principal's stubs and
  skills in scope
- `list_api()`, `read_api(name)`, `search_api(query)`: browse those stubs and skills,
  rendered as Python signatures
- `save_skill(name, summary, run=...)`: keep a function from a script that ran as a
  skill, for the agent's later scripts, with no approval (`broker/skills.py`)
- `promote_skill(name)`: ask the user to promote a kept skill, to share it with other
  agents or trust one that writes

The gateway holds no service credentials. It asks the runner to run scripts, and the
broker for the API documentation.

The tokens file holds a SHA-256 hash of each principal's token, never the token:

    {"principals": {"claude-code": "<sha256 hex of the token>"}}

Make a token and its hash with `maji-gateway --new-token`.

A session is what a grant "for this session" and `maji.save` hold to. A client on an
earlier protocol revision has an MCP session, which this server issues. A client on a
revision without them, such as 2026-07-28, which Claude Code speaks, sends each
request on its own, so its app may name a session in `X-Maji-Session` instead
(`NamedSessions`). A client with neither has no session.

It logs each session's life, so its log says how long sessions last: when one starts,
which principal and app it is, when the client ends it, and when it ends for going
idle (`IDLE_SECONDS`) or comes back after that. Session ids are logged by their first
8 characters.

Run it with:

    maji-gateway --tokens /run/secrets/gateway_tokens.json --host 0.0.0.0 --port 8765
"""

# system imports
import argparse
import hashlib
import hmac
import json
import logging
import re
import secrets
import sys
import time
from collections import OrderedDict
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any

# 3rd party imports
import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

# project imports
from maji_mcp.broker.store import IDLE_SECONDS
from maji_mcp.common.names import session_name
from maji_mcp.common.protocol import MajiError, call_unix

log = logging.getLogger("maji.gateway")

# What an MCP client says it is goes on items as it gave it, so it is cut short and
# kept to printable characters.
#
MAX_CLIENT_TEXT = 100

# The SDK's session ids are 32 hex digits (`uuid4().hex`). The gateway logs only their
# first 8, enough to follow one session through the log.
#
SESSION_ID = re.compile(r"\b[0-9a-f]{32}\b")

# Sessions already logged, by key, so each is logged once. Forgotten all at once past
# this many, after which a session still going is logged again on its next call.
#
MAX_NOTED = 10_000
_noted: set[str] = set()

# A session a client names: its header, the ids it may carry, and how many the gateway
# keeps at once (`NamedSessions`). Claude Code's helper makes 32 hex digits
# (`bin/maji-headers`).
#
SESSION_HEADER = "x-maji-session"
NAMED_SESSION = re.compile(r"[A-Za-z0-9_-]{16,128}")
MAX_NAMED_SESSIONS = 10_000

INSTRUCTIONS = """\
maji-mcp runs Python you write against typed APIs for your services, in a sandbox, and
returns only what the script prints.

Use run_python instead of a series of tool calls when you would otherwise:
- call the same tool many times, once per item, page or id
- read a large result to keep a little: a count, a filter, a maximum
- copy output from one tool into another's arguments: do the join in code
- regenerate a whole record to change part of it
Stay with a direct call for one call with a small result, or for content you have to
read yourself to judge.

Estimate before choosing, in output tokens: cost ≈ 0.45·W + 0.04·R + 400·k +
0.1·(W + R)·(1 + t/25), with W characters written, R characters read into context,
k tool calls and t turns left. Code pays when it is used more than (first-time cost ÷
saving per use) times.

In a script, each namespace is a global: sentry.issues(project="api"). Call
list_api() to see what you may call, and read_api("sentry") for signatures. A failed
call raises maji.StubError with .code (not_found, invalid_argument, permission_denied,
rate_limited, unavailable, ...). Print only what the answer needs.

Each script starts fresh. maji.save(name, value) keeps a result for this session's
later scripts, and maji.load(name) returns it, so a check needn't fetch again.
maji.parallel(fn, items) runs independent calls at once, such as one query per
environment; org calls still run one at a time.

A script can also read folders and files you name in run_python's `paths`, such as
repositories or log captures, once the user grants them; run_python says how.

Beyond the standard library, a script can import libraries for code, text and data:
YAML, Jinja templates, Markdown, syntax trees for several languages, diffs, fuzzy
matching, duckdb and pandas. read_api("python") lists them.

Skills are functions kept for reuse, called in a script like stubs:
skills.culprits(project="api"). list_api lists those you may call, and read_api shows
each one's code. Look for one before writing a script for a task that sounds
common. When a function you wrote and ran would be worth having again, keep it with
save_skill: it is yours in later sessions too, and nobody has to approve it. Ask to
promote one with promote_skill only when the user wants it shared with other agents,
or for a skill that writes.
"""

# How many of a run's stubs and skills its result names, before saying how many more,
# and how many of the files a mount left out.
#
MAX_CALLED_SHOWN = 12
MAX_LEFT_OUT_SHOWN = 5

# The scripts kept for `save_skill`: how many a session, and how many bytes in all.
#
MAX_KEPT_PER_SESSION = 20
MAX_KEPT_BYTES = 32 * 1024 * 1024

# A run is named by its id, or by its start, as the result shows it: at least this
# many characters.
#
MIN_RUN_PREFIX = 8

# How long a stopping gateway waits for the requests and streams still open: time
# for a call that is about to answer, and short beside the ten seconds Docker allows
# a container before killing it.
#
SHUTDOWN_SECONDS = 3


###############################################################################
#
class TokenAuth:
    """
    ASGI middleware that admits only requests bearing a known principal's token.

    The principal goes into the ASGI scope's state, where the tools read it. Tokens are
    compared by hash, in constant time.

    Args:
        app: the MCP app to protect
        hashes: each principal's token hash, as SHA-256 hex
    """

    def __init__(self, app: Any, hashes: dict[str, str]) -> None:
        self.app = app
        self.hashes = hashes

    def principal(self, authorization: str) -> str | None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None
        digest = hashlib.sha256(token.strip().encode()).hexdigest()
        found = None
        for name, expected in self.hashes.items():
            if hmac.compare_digest(digest, expected):
                found = name
        return found

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        # Only the server's own startup and shutdown pass unchecked. Anything else that
        # isn't plain HTTP, such as a websocket, is refused, so a route added later
        # can't slip past the token check.
        #
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        headers = dict(scope.get("headers") or [])
        principal = self.principal(headers.get(b"authorization", b"").decode())
        if principal is None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {"type": "http.response.body", "body": b'{"error":"unauthorized"}'}
            )
            return
        scope.setdefault("state", {})["maji_principal"] = principal
        await self.app(scope, receive, send)


def principal_of(ctx: Context) -> str:
    """Return the principal the request was authenticated as."""
    request = ctx.request_context.request
    principal = getattr(request, "scope", {}).get("state", {}).get("maji_principal")
    if not principal:
        raise MajiError("unauthenticated", "no principal for this request")
    return str(principal)


async def ask(
    sock: Path, method: str, params: dict[str, Any], timeout: float = 60.0
) -> Any:
    """
    Call the runner or the broker for a tool, giving the agent the reason for a
    refusal.

    The SDK keeps an exception's text from the client unless it is a `ToolError`, so
    each refusal is raised as one, with its code, such as a path never mounted and
    what to do instead. A service that failed unexpectedly reports only the kind of
    exception, so that is all an internal failure says.

    Raises:
        ToolError: the service refused, or failed
    """
    try:
        return await call_unix(sock, method, params, timeout=timeout)
    except MajiError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc


def client_of(ctx: Context) -> dict[str, str] | None:
    """
    Return what the agent's app said it is when it connected: its name and version.

    Returns:
        The name and version, as the client gave them, cut to `MAX_CLIENT_TEXT`
        printable characters each. Self-reported, so shown to you and never trusted;
        None when the client gave none
    """
    params = getattr(ctx.session, "client_params", None)
    info = getattr(params, "client_info", None)
    if info is None or not info.name:
        return None
    client = {"name": _printable(info.name)}
    if info.version:
        client["version"] = _printable(info.version)
    return client


def _printable(text: str) -> str:
    return "".join(c for c in str(text) if c.isprintable())[:MAX_CLIENT_TEXT]


def note_session(ctx: Context, principal: str, key: str | None) -> None:
    """
    Log an MCP session the first time it makes a tool call: its id and key, by their
    first 8 characters, its name for people, its principal, and the app it says it is.
    A session a client names is logged as it begins (`session_of`).

    The SDK logs when each session starts and ends by its id; the key is what an
    item shows as its requester's session, so this line ties the two together.
    """
    headers = getattr(ctx.request_context.request, "headers", None)
    session_id = headers.get("mcp-session-id") if headers is not None else None
    if not session_id or not key or key in _noted:
        return
    if len(_noted) >= MAX_NOTED:
        _noted.clear()
    _noted.add(key)
    log.info(
        "session %s (key %s, %s) of %s, app %s",
        session_id[:8],
        key[:8],
        session_name(key),
        principal,
        _app(ctx),
    )


def _app(ctx: Context) -> str:
    """The app the client says it is, with its version, for the log."""
    client = client_of(ctx) or {}
    return " ".join(client.get(k, "") for k in ("name", "version")).strip() or "unnamed"


class ShortIds(logging.Filter):
    """Shorten each session id in a log line to its first 8 characters."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = SESSION_ID.sub(lambda m: m.group()[:8], record.getMessage())
        record.args = None
        return True


def log_sessions() -> None:
    """
    Log MCP sessions' lives at INFO, with timestamps, to standard error.

    Uvicorn logs only warnings here, and the SDK's session lines are INFO, so the
    SDK's session manager and transport loggers, and the gateway's own, get a handler
    of their own.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    handler.addFilter(ShortIds())
    for name in (
        "mcp.server.streamable_http_manager",
        "mcp.server.streamable_http",
        log.name,
    ):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False


class NamedSessions:
    """
    The sessions clients name in `X-Maji-Session`, each kept until it goes an hour
    without running a script.

    A client on a protocol revision without MCP sessions sends each request on its
    own, so its app names the session: Claude Code's `headersHelper` makes a new id
    each time it connects (`bin/maji-headers`). The key the broker sees is made here,
    from the principal, the id, and a nonce drawn when the session began. So a
    session ends, and its grants and saved values with it, when its app connects
    again with a new id, when it goes `IDLE_SECONDS` without a script, or when the
    gateway restarts; the same id then begins a new one.

    The id is the client's own, so it tells one session of an agent's app from
    another, not one client from another holding the same token. Keys are per
    principal, so a client reaches only sessions under its own token.

    Args:
        idle_seconds: how long a session lasts without a script
        max_sessions: how many to keep at once; past that, the one unused longest is
            forgotten, and begins afresh if it comes back
        clock: seconds, by which idleness is measured
    """

    def __init__(
        self,
        idle_seconds: float = IDLE_SECONDS,
        max_sessions: int = MAX_NAMED_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.idle_seconds = idle_seconds
        self.max_sessions = max_sessions
        self.clock = clock
        self._held: OrderedDict[tuple[str, str], tuple[str, float]] = OrderedDict()

    def key(self, principal: str, named: str) -> tuple[str, str | None]:
        """
        Return the key of the session a client named, as it runs a script.

        Returns:
            The key, and how the session began if this call began it: `new` for an
            id this gateway hasn't kept, `idle` for one that went too long without
            a script; None for a session already going
        """
        now = self.clock()
        held = self._held.pop((principal, named), None)
        if held is not None and now - held[1] <= self.idle_seconds:
            key, began = held[0], None
        else:
            began = "new" if held is None else "idle"
            nonce = secrets.token_hex(16)
            key = hashlib.sha256(
                f"{principal}\0{SESSION_HEADER}\0{named}\0{nonce}".encode()
            ).hexdigest()
        self._held[(principal, named)] = (key, now)
        while len(self._held) > self.max_sessions:
            self._held.popitem(last=False)
        return key, began


def session_of(ctx: Context, principal: str, named: NamedSessions) -> str | None:
    """
    Return the key the broker keeps this request's session under, if it has one.

    An MCP session comes first. Its id is one this server issued; the session manager
    answers an unknown id with 404 before any tool runs. Without one, the session is
    the one the client names in `X-Maji-Session`. Either way the key is hashed with
    the principal, so an id presented with another principal's token reaches a
    different session.

    Returns:
        A SHA-256 in hex, or None for a client with neither

    Raises:
        ToolError: `X-Maji-Session` isn't an id the gateway takes
    """
    headers = getattr(ctx.request_context.request, "headers", None)
    if headers is None:
        return None
    session_id = headers.get("mcp-session-id")
    if session_id:
        return hashlib.sha256(f"{principal}\0{session_id}".encode()).hexdigest()
    named_id = headers.get(SESSION_HEADER)
    if named_id is None:
        return None
    if not NAMED_SESSION.fullmatch(named_id):
        raise ToolError(
            "invalid_argument: X-Maji-Session must be 16 to 128 letters, digits, "
            "'-' or '_'"
        )
    key, began = named.key(principal, named_id)
    if began == "new":
        log.info(
            "session %s (key %s, %s) of %s, app %s, named by its app",
            named_id[:8],
            key[:8],
            session_name(key),
            principal,
            _app(ctx),
        )
    elif began == "idle":
        log.info(
            "session %s of %s went an hour without a script, so a new one began "
            "(key %s, %s), app %s",
            named_id[:8],
            principal,
            key[:8],
            session_name(key),
            _app(ctx),
        )
    return key


class RecentScripts:
    """
    The scripts each session ran that succeeded, newest kept, so `save_skill` can
    name a run instead of sending its code again.

    Writing costs an agent far more than reading (`INSTRUCTIONS`), and the function
    it wants to keep is already in a script it ran. The broker checks the script
    against its own record of the run, so what is kept here is only a copy.

    Kept in memory: the last `per_session` of each session, and `max_bytes` in all,
    the session used longest ago dropped first. A restarted gateway keeps none, and
    an agent then sends the code.

    Args:
        per_session: how many scripts to keep for each session
        max_bytes: how many bytes of scripts to keep in all
    """

    def __init__(
        self, per_session: int = MAX_KEPT_PER_SESSION, max_bytes: int = MAX_KEPT_BYTES
    ) -> None:
        self.per_session = per_session
        self.max_bytes = max_bytes
        self._kept: OrderedDict[str, OrderedDict[str, str]] = OrderedDict()
        self._bytes = 0

    def keep(self, session: str, run_id: str, code: str) -> None:
        runs = self._kept.pop(session, None) or OrderedDict()
        runs[run_id] = code
        self._bytes += len(code.encode())
        while len(runs) > self.per_session:
            _, dropped = runs.popitem(last=False)
            self._bytes -= len(dropped.encode())
        self._kept[session] = runs
        while self._bytes > self.max_bytes and self._kept:
            oldest = next(iter(self._kept))
            gone = self._kept[oldest]
            _, dropped = gone.popitem(last=False)
            self._bytes -= len(dropped.encode())
            if not gone:
                del self._kept[oldest]

    def find(self, session: str, run: str) -> tuple[str, str] | None:
        """
        A kept run of this session by its id, or by a start of it at least
        `MIN_RUN_PREFIX` long that only one kept run has.

        Returns:
            The run's full id and its script, or None
        """
        runs: dict[str, str] = self._kept.get(session) or {}
        if len(run) < MIN_RUN_PREFIX:
            return None
        found = [(rid, code) for rid, code in runs.items() if rid.startswith(run)]
        return found[0] if len(found) == 1 else None


###############################################################################
#
def build_server(runner_sock: Path, broker_control: Path) -> MCPServer:
    """
    Build the gateway's MCP server.

    Args:
        runner_sock: the runner's socket, for `run_python`
        broker_control: the broker's control socket, for the API tools
    """
    mcp = MCPServer("maji-mcp", instructions=INSTRUCTIONS, version=version("maji-mcp"))
    named = NamedSessions()
    recent = RecentScripts()

    @mcp.tool()
    async def run_python(
        code: str,
        ctx: Context,
        timeout_s: int = 60,
        paths: list[str] | None = None,
        why: str | None = None,
    ) -> str:
        """
        Run a Python script in the sandbox and return what it prints.

        Each stub namespace you may call is a global, such as `sentry`. The script has
        no network and no credentials; stubs are its only way out. Print only what the
        answer needs: the output is capped at 64 KB. A run stops after `timeout_s`
        seconds, 60 unless you ask for more, up to 600.

        Each script starts fresh. To check a result later without fetching it again,
        keep it: `maji.save("issues", issues)`, then `maji.load("issues")` in a later
        script of this session. `maji.parallel(fn, items)` runs independent calls at
        once and returns their results in order.

        Kept skills are functions in `skills`: `skills.culprits(project="api")`. To
        keep a function this script defines, call it here to see it work, then pass
        this run's id to save_skill. The result's last line names the stubs and
        skills the script called.

        `paths` names up to 10 folders or files the script reads, as absolute paths
        under the user's home directory: a repository, a folder inside one, a folder of
        log captures. Ask for only what the script needs; nothing beyond each path is
        mounted. The home directory itself is never mounted, so don't ask for it to
        search: ask the user where what you're looking for is. Nor are some places
        the user has listed; when one is refused, ask them where else to look. Each
        path is mounted read-only at `/mnt/<its last part>`, and `maji.mounts()`
        lists them. `why` says in a sentence or two why the script needs them; the
        user reads it, labelled as yours.

        A mounted folder leaves some files out: those named like secrets, such as
        `.env` or a private key, and places the user keeps to themselves. Each reads
        as empty, and the result says which. If the script needs one, name that file
        in `paths`, and the user is asked about it on its own.

        The first time, the user is asked to grant the paths, and the script doesn't
        run: the result names the queue item. Run the same request again once the
        user has decided; until then it returns the same item, so don't rerun it in
        a loop. A grant on a folder covers the folders in it. A path the user refused
        stays refused, unless they let you ask again.

        For mounted files: `maji.git_log(name, since=..., paths=...)` returns each
        commit with the files it changed, from one git call; `maji.blame(name, path,
        line)` the commit behind a line; `maji.sqlite(name, path)` opens a database
        read-only.

        Beyond the standard library, a script can import `yaml` (PyYAML), `jinja2`
        (Jinja2), `markdown_it` (markdown-it-py), `pygments` (Pygments),
        `tree_sitter` (tree-sitter, with tree-sitter-javascript,
        tree-sitter-typescript, tree-sitter-go, tree-sitter-rust, tree-sitter-ruby
        and tree-sitter-perl), `unidiff`, `pathspec`, `rapidfuzz` (RapidFuzz),
        `duckdb`, `pandas` and `git` (GitPython). read_api("python") says what each
        is for, and read_api("python.<name>") where to start and what to watch for;
        `help(...)` on a function, in a script, shows the rest.
        """
        principal = principal_of(ctx)
        params: dict[str, Any] = {
            "principal": principal,
            "runtime": "python",
            "code": code,
            "timeout_s": timeout_s,
        }
        session = session_of(ctx, principal, named)
        note_session(ctx, principal, session)
        if session:
            params["session"] = session
        client = client_of(ctx)
        if client:
            params["client"] = client
        if paths is not None:
            params["paths"] = paths
        if why is not None:
            params["why"] = why
        result = await ask(runner_sock, "run", params, timeout=700)
        if (
            session
            and "pending" not in result
            and result.get("exit_code") == 0
            and not result.get("timed_out")
        ):
            recent.keep(session, result["run_id"], code)
        return format_result(result, session)

    def asking(ctx: Context) -> dict[str, Any]:
        """Who is asking, as the broker's API calls take it: principal and session."""
        principal = principal_of(ctx)
        session = session_of(ctx, principal, named)
        return {"principal": principal, **({"session": session} if session else {})}

    @mcp.tool()
    async def list_api(ctx: Context) -> str:
        """List the stub namespaces, stubs and skills you may call from run_python."""
        listing = await ask(broker_control, "list_api", asking(ctx))
        lines = [f"{ns} {version}" for ns, version in listing["namespaces"].items()]
        lines += [f"  {s['name']}: {s['summary']}" for s in listing["stubs"]]
        return "\n".join(
            (lines or ["No stubs are granted to you."])
            + ['python: the libraries a script can import; read_api("python")']
        )

    @mcp.tool()
    async def read_api(name: str, ctx: Context) -> str:
        """
        Show the Python signature and documentation of a stub, or of a whole namespace.
        For a skill, `skills.<name>`, it shows its code too, and for `skills`, every
        skill's. `python` lists the libraries a script can import, and
        `python.<name>`, such as `python.pandas`, shows where to start with one and
        what to watch for.
        """
        doc: str = await ask(broker_control, "read_api", {**asking(ctx), "name": name})
        return doc

    @mcp.tool()
    async def search_api(query: str, ctx: Context) -> str:
        """
        Find stubs and skills whose name, summary or parameters mention every word of
        the query, and libraries a script can import whose name or summary does.
        """
        found = await ask(broker_control, "search_api", {**asking(ctx), "query": query})
        return "\n".join(f"{s['signature']}\n  {s['summary']}" for s in found) or (
            "No stubs, skills or libraries match."
        )

    @mcp.tool()
    async def save_skill(
        name: str,
        summary: str,
        ctx: Context,
        run: str | None = None,
        code: str | None = None,
    ) -> str:
        """
        Keep a function you wrote as a skill, so later scripts call it as
        `skills.<name>(...)` instead of writing it again.

        Write it as one function named `name`, with typed parameters (str, int,
        float, bool, list, dict, Any or None, as in list[dict] or str | None), a
        return type, and a docstring saying what it returns. Call it in a run_python
        script to see it work, then name that run in `run`: the id its result ends
        with, such as "1a2b3c4d". Only the script's imports, functions, classes and
        UPPER_CASE constants are kept; the lines that called the function and
        printed are left out. Send the code in `code` instead only for a run this
        session no longer has.

        `summary` is one line saying what it does and when to use it. You read it in
        list_api, and so does every agent once the user promotes the skill.

        It's yours to call at once, in this session and later ones, and nothing
        waits for the user. Keeping it again with changes makes a new version. Ask
        with promote_skill only to share it with other agents, or for one that
        writes.
        """
        who = asking(ctx)
        if (run is None) == (code is None):
            raise ToolError(
                "invalid_argument: name the run, or send the code; not both"
            )
        script = code
        if run is not None:
            session = who.get("session")
            found = recent.find(session, run) if session else None
            if found is None:
                raise ToolError(
                    f"invalid_argument: no run {run} of this session that succeeded; "
                    "run it again, or send the code"
                )
            run, script = found
        params: dict[str, Any] = {
            **who,
            "name": name,
            "summary": summary,
            "script": script,
        }
        if run is not None:
            params["run"] = run
        saved = await ask(broker_control, "save_skill", params)
        return format_saved(saved)

    @mcp.tool()
    async def promote_skill(name: str, ctx: Context, why: str | None = None) -> str:
        """
        Ask the user to promote the version of a skill you kept last, so every agent
        is offered it.

        Ask only when it changes something: the user wants the skill shared with
        other agents, or it writes, and the user should review what it reaches
        before it is trusted. A skill only you use needs no promotion. `why` says
        why it's worth promoting; the user reads it, labelled as yours.

        The result names the item in the user's queue. Keeping a changed version and
        asking again replaces the item still waiting.
        """
        params: dict[str, Any] = {**asking(ctx), "name": name}
        if why is not None:
            params["why"] = why
        client = client_of(ctx)
        if client:
            params["client"] = client
        asked = await ask(broker_control, "promote_skill", params)
        return format_promoted(asked)

    return mcp


def build_app(mcp: MCPServer, hashes: dict[str, str]) -> TokenAuth:
    """
    Serve the gateway over streamable HTTP, admitting only known principals' tokens.

    Args:
        mcp: the server from `build_server`
        hashes: each principal's token hash, as SHA-256 hex
    """
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )

    # Stateful, so a client on an earlier protocol revision gets an MCP session with an
    # id, and its scripts can keep values for each other (`maji.save`). The SDK ends a
    # session that has been idle for 30 minutes, after which a client starts a new one;
    # idling for as long as the broker keeps saved values means they stay reachable for
    # that long. The SDK serves a 2026-07-28 request on its own whatever this says, so
    # such a client's session is the one it names (`NamedSessions`).
    #
    app = mcp.streamable_http_app(
        stateless_http=False,
        transport_security=security,
        session_idle_timeout=IDLE_SECONDS,
    )
    return TokenAuth(app, hashes)


def format_result(result: dict[str, Any], session: str | None = None) -> str:
    """
    Render a run's result for the agent: the output first, then a one-line status.

    stderr is included only when the run failed, since a successful script's warnings
    are rarely worth the tokens. A mount that left files out says which, so a script
    that read one as empty isn't taken to have found it so. A run waiting on a grant gets only that: what to
    decide, that the script hasn't run, and the name the queue shows for its session,
    so the agent can tell the user which item is its own.

    Args:
        result: the runner's result
        session: the key of the run's session, if it has one
    """
    pending = result.get("pending")
    if pending is not None:
        shown = (
            f", which shows this session as {session_name(session)}" if session else ""
        )
        return (
            "[not run: the user hasn't granted "
            + ", ".join(pending["paths"])
            + f". Waiting on {', '.join(pending['items'])} in their queue{shown}. Run"
            " the same request again once they've decided; until then it returns"
            " this again.]"
        )
    parts = [result["stdout"].rstrip("\n")] if result["stdout"] else []
    if result["truncated"]:
        parts.append("[output truncated at the cap; print less]")
    failed = result["timed_out"] or result["exit_code"] != 0
    if failed and result["stderr"]:
        parts.append(result["stderr"].rstrip("\n"))
    for name, out in (result.get("left_out") or {}).items():
        parts.append(f"[left out of /mnt/{name}, read as empty: {_left_out(out)}]")
    status = "timed out" if result["timed_out"] else f"exit {result['exit_code']}"
    mounted = result.get("mounts") or {}
    if mounted:
        status += ", mounted " + ", ".join(f"/mnt/{name}" for name in mounted)
    status += f", {result['duration_ms']} ms"
    called = result.get("called") or []
    if called:
        status += "; called " + _called(called)
    parts.append(f"[run {result['run_id'][:8]}: {status}]")
    return "\n".join(parts)


def _left_out(out: dict[str, Any]) -> str:
    """
    Name what a mount left out: the files named like secrets, and how many more
    there are, counting the places the user keeps to themselves, which aren't named.
    """
    files = [str(f) for f in out["files"][:MAX_LEFT_OUT_SHOWN]]
    more = int(out["count"]) - len(files)
    if not files:
        return f"{more} {'place' if more == 1 else 'places'}"
    return ", ".join(files) + (f", and {more} more" if more > 0 else "")


def _called(called: list[dict[str, Any]]) -> str:
    """
    Name what a run called, in the order it first called each: `sentry.events ×40
    (3 failed)`, so the user sees in the agent's transcript which tools a script
    used.
    """
    shown = []
    for entry in called[:MAX_CALLED_SHOWN]:
        text = str(entry["name"])
        if entry["calls"] > 1:
            text += f" ×{entry['calls']}"
        if entry["failed"]:
            text += (
                " (failed)"
                if entry["failed"] == entry["calls"]
                else f" ({entry['failed']} failed)"
            )
        shown.append(text)
    more = len(called) - MAX_CALLED_SHOWN
    return ", ".join(shown) + (f", and {more} more" if more > 0 else "")


def format_saved(saved: dict[str, Any]) -> str:
    """
    Render a kept skill for the agent: what was kept, what it reaches, and that it
    can call it now.
    """
    lines = [
        f"Kept skills.{saved['name']}, version {saved['version']}: {saved['signature']}"
    ]
    if saved.get("dropped"):
        spans = ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in saved["dropped"])
        lines.append(f"Left out of the script: lines {spans}.")
    reach = "Reads with " + (
        ", ".join(saved["reads"]) if saved["reads"] else "no stubs"
    )
    if saved.get("writes"):
        reach += "; writes with " + ", ".join(saved["writes"])
    lines.append(reach + ".")
    for finding in saved.get("dynamic") or []:
        lines.append(f"Can't be followed from its code: {finding}.")
    if saved["state"] == "promoted":
        lines.append("This version is the promoted one, so every agent has it.")
    elif saved["state"] == "demoted":
        lines.append(
            f"The user demoted this version on {saved.get('demoted_on')}, so no agent "
            "runs it, you included. Change it and keep it again to run it as yours."
        )
        if saved.get("note"):
            lines.append(f"The user's note on demoting it: {saved['note']}")
        return "\n".join(lines)
    else:
        lines.append(
            "It's yours to call now, in this session and later ones. Other agents get "
            "it only if the user promotes it."
        )
    if saved.get("waiting"):
        lines.append(
            f"Your request to promote an earlier version still waits on "
            f"{saved['waiting']}; ask again with promote_skill to put this one in its "
            "place."
        )
    if saved.get("note"):
        lines.append(f"The user's note on an earlier version: {saved['note']}")
    return "\n".join(lines)


def format_promoted(asked: dict[str, Any]) -> str:
    """
    Render a request to promote a skill for the agent: the item it waits on, which
    the user's queue shows under this session's name, and why it is as risky as it
    is.
    """
    named = f"skills.{asked['name']}, version {asked['version']}"
    shown = (
        f", which shows this session as {asked['session_name']}"
        if asked.get("session_name")
        else ""
    )
    if asked["state"] == "promoted":
        lines = [f"{named} is already promoted, so every agent has it."]
    elif asked.get("risk") is None:
        lines = [
            f"{named} already waits on {asked['item']} in the user's queue{shown}."
        ]
    else:
        lines = [f"Asked: {named} waits on {asked['item']} in the user's queue{shown}."]
        if asked.get("risk") == "high" and asked.get("writes"):
            lines.append(
                "It writes with " + ", ".join(asked["writes"]) + ", so it is high "
                "risk, and the user signs it with their passkey."
            )
        elif asked.get("risk") == "high":
            lines.append(
                "What it reaches can't all be read from its code, so it is high risk, "
                "and the user signs it with their passkey."
            )
        else:
            lines.append("It only reads, so it is an ordinary item for the user.")
    if asked.get("note"):
        lines.append(f"The user's note on an earlier version: {asked['note']}")
    return "\n".join(lines)


###############################################################################
#
def main() -> None:
    parser = argparse.ArgumentParser(description="the maji-mcp gateway")
    parser.add_argument(
        "--tokens", type=Path, default=Path("/run/secrets/gateway_tokens.json")
    )
    parser.add_argument(
        "--runner-sock", type=Path, default=Path("/run/maji-runner/runner.sock")
    )
    parser.add_argument(
        "--broker-control", type=Path, default=Path("/run/maji-control/control.sock")
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--new-token",
        metavar="PRINCIPAL",
        help="print a new token for PRINCIPAL, and the line for the tokens file, then exit",
    )
    args = parser.parse_args()
    if args.new_token:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        print(f"token for {args.new_token}: {token}", file=sys.stderr)
        print(json.dumps({args.new_token: digest}))
        return

    hashes = json.loads(args.tokens.read_text())["principals"]
    log_sessions()
    app = build_app(build_server(args.runner_sock, args.broker_control), hashes)
    # An app holds a stream open with the gateway for as long as the app runs, and
    # uvicorn's stop waits for every open connection. The bound ends the wait, so
    # the gateway stops by itself well inside the time Docker allows a container.
    #
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        timeout_graceful_shutdown=SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()
