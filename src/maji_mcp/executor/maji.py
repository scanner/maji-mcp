"""
The stub client: how a script in the executor calls the broker.

This file is copied into the Python runtime image as the module `maji`, so it uses the
standard library only. A script calls a stub as a function in its namespace:

    issues = sentry.issues(project="api", query="is:unresolved", period="14d")

which sends one JSON-RPC request over the broker's socket. The run's token, set by the
runner, identifies the run. The broker works out the principal from it, so nothing a
script sends can change whose permissions it runs with.

Calls from different threads run at the same time, each thread on its own connection;
`parallel` is the short way to use that. The broker works on at most 8 of a run's calls
at once, and the rest wait.

`save` and `load` keep values for the session's later scripts, in the broker.

`skills` holds the functions saved in the library, called like stubs:
`skills.culprits(project="api")`. Each skill's code comes from the broker the first
time a run calls it, and runs here, in the script's own process.

The paths a run was granted are mounted read-only under `/mnt`. `mounts`, `git`,
`git_log`, `blame` and `sqlite` work on them here in the executor, without the broker.
"""

# system imports
import json
import linecache
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import types
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SOCKET = os.environ.get("MAJI_BROKER_SOCKET", "/run/maji/call.sock")
TOKEN = os.environ.get("MAJI_RUN_TOKEN", "")
MAX_MESSAGE = 16 * 1024 * 1024
MOUNTS = Path("/mnt")

# A mounted repository's own config can name commands for git to run: a filesystem
# monitor, hooks, an external diff, a textconv filter. In the executor they reach
# nothing but the run's stubs, but they could change what git reports. Settings given
# with `-c` outrank the repository's config, so these turn them off. The runtime
# image's environment sets the same for every git process; passing them here too
# keeps them wherever this client runs. `safe.directory` is needed because the files
# belong to you and the executor runs as user 10001. No setting turns off an external
# diff or a textconv filter, so the helpers pass `--no-ext-diff` and `--no-textconv`.
#
GIT_SAFE = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "safe.directory=*",
    "-c",
    "core.pager=cat",
)


###############################################################################
#
class StubError(Exception):
    """
    A stub call that failed, with the code the broker or the service returned.

    The codes are those of the server contract: `conflict`, `not_found`,
    `invalid_argument`, `unavailable`, `internal`, plus the broker's own
    `unauthenticated`, `permission_denied` and `rate_limited`.
    """

    def __init__(self, code: str, message: str, data: dict[str, Any]) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class _Connection:
    """One connection to the broker, opened on first use and kept for the run."""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._stream: Any = None
        self._next_id = 0

    def request(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        """
        Send one request and return its result.

        Args:
            method: the broker's method
            params: its parameters; the run's token is added
            timeout: how long to wait, in seconds; without one, as long as it takes
        """
        if self._sock is None:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.settimeout(timeout)
            self._sock.connect(SOCKET)
            self._stream = self._sock.makefile("rb")
        self._sock.settimeout(timeout)
        self._next_id += 1
        message = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": {"token": TOKEN, **params},
        }
        self._sock.sendall(json.dumps(message).encode() + b"\n")
        raw = self._stream.readline(MAX_MESSAGE)
        if not raw:
            raise StubError("unavailable", "the broker closed the connection", {})
        reply = json.loads(raw)
        if "error" in reply:
            data = dict(reply["error"].get("data") or {})
            code = data.pop("code", "internal")
            raise StubError(code, reply["error"].get("message", ""), data)
        return reply["result"]


# A connection carries one request at a time, so each thread gets its own.
#
_local = threading.local()


def _connection() -> _Connection:
    if not hasattr(_local, "connection"):
        _local.connection = _Connection()
    connection: _Connection = _local.connection
    return connection


def call(name: str, /, **args: Any) -> Any:
    """
    Call a stub by its full name, such as `sentry.issues`.

    `name` is positional-only, so a stub's own parameter called `name`, such as
    `maji.save`'s, passes through in `args`.

    Raises:
        StubError: the broker refused the call, or the service failed
    """
    return _connection().request("call", {"stub": name, "args": args})


def describe() -> list[dict[str, Any]]:
    """Return the stubs this run may call: name, signature and summary."""
    result: list[dict[str, Any]] = _connection().request("describe", {})
    return result


def parallel(
    fn: Callable[[Any], Any], items: Iterable[Any], workers: int = 8
) -> list[Any]:
    """
    Return `[fn(item) for item in items]`, calling `fn` on several items at once.

    For independent stub calls, such as one query per environment:

        counts = maji.parallel(lambda env: sentry.issues(project="api",
                               environments=[env]), envs)

    Results come back in the order of `items`. If any call raises, the first error is
    raised, for the first item that failed, once every call has finished.
    """
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as pool:
        return list(pool.map(fn, items))


def save(name: str, value: Any) -> dict[str, Any]:
    """Keep a JSON value for this session's later scripts (the `maji.save` stub)."""
    result: dict[str, Any] = call("maji.save", name=name, value=value)
    return result


def load(name: str) -> Any:
    """Return a value an earlier script in this session saved (`maji.load`)."""
    return call("maji.load", name=name)


def saved() -> list[dict[str, Any]]:
    """List this session's saved values, with their size and when each was saved."""
    result: list[dict[str, Any]] = call("maji.saved")
    return result


###############################################################################
#
def mounts() -> dict[str, Path]:
    """
    Return the paths this run has mounted, by name: `{"api": Path("/mnt/api")}`.

    Each is read-only. Its name is the last part of the path asked for; when two end
    alike, the later ones get `-2`, `-3` and so on.
    """
    if not MOUNTS.is_dir():
        return {}
    return {p.name: p for p in sorted(MOUNTS.iterdir())}


def _mounted(name: str) -> Path:
    path = mounts().get(name)
    if path is None:
        raise StubError(
            "not_found", f"nothing is mounted as {name!r}", {"mounts": sorted(mounts())}
        )
    return path


def git(name: str, *args: str) -> str:
    """
    Run git in the mounted repository `name`, and return what it prints.

    For example `maji.git("api", "rev-parse", "HEAD")`. The repository is read-only,
    so commands that write fail. Pass `--no-ext-diff --no-textconv` to anything that
    shows a diff, so the repository's own config can't change it.

    Raises:
        StubError: `not_found` when nothing is mounted as `name`, and
            `invalid_argument` with git's message when git fails
    """
    repo = _mounted(name)
    try:
        done = subprocess.run(
            ["git", *GIT_SAFE, "-C", str(repo), *args],
            capture_output=True,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError:
        raise StubError("unavailable", "git isn't in this runtime image", {}) from None
    if done.returncode != 0:
        message = done.stderr.decode(errors="replace").strip()
        raise StubError(
            "invalid_argument", f"git {args[0] if args else ''}: {message}", {}
        )
    return done.stdout.decode(errors="replace")


def git_log(
    name: str,
    *,
    rev: str = "HEAD",
    since: str | None = None,
    until: str | None = None,
    paths: Sequence[str] = (),
    max_count: int | None = None,
) -> list[dict[str, Any]]:
    """
    Return the commits reachable from `rev` in mounted repository `name`, newest
    first, each with the files it changed.

        for c in maji.git_log("api", since="90 days ago", paths=["src/"]):
            print(c["sha"][:10], c["date"], c["subject"], len(c["files"]))

    Args:
        name: the mount's name, as `mounts()` gives it
        rev: a branch, tag or commit
        since, until: anything git's `--since` takes, such as `2026-10-01` or
            `90 days ago`
        paths: only commits that changed these paths
        max_count: at most this many commits

    Returns:
        Each commit as `sha`, `author`, `email`, `date` (the author's, ISO 8601),
        `subject`, and `files`: a list of `path`, `added` and `removed` line counts,
        which are None for a binary file. A rename is a removal and an addition. A
        merge lists no files.
    """
    args = [
        "log",
        "-z",
        "--format=%x1e%H%x1f%an%x1f%ae%x1f%aI%x1f%s%x1f",
        "--numstat",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
    ]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    if max_count is not None:
        args.append(f"--max-count={int(max_count)}")
    args += ["--end-of-options", rev, "--", *paths]

    commits: list[dict[str, Any]] = []
    for record in git(name, *args).split("\x1e")[1:]:
        sha, author, email, date, subject, rest = record.split("\x1f", 5)
        files = []
        for entry in rest.lstrip("\0").removeprefix("\n").split("\0"):
            if not entry:
                continue
            added, removed, path = entry.split("\t", 2)
            files.append(
                {
                    "path": path,
                    "added": None if added == "-" else int(added),
                    "removed": None if removed == "-" else int(removed),
                }
            )
        commits.append(
            {
                "sha": sha,
                "author": author,
                "email": email,
                "date": date,
                "subject": subject,
                "files": files,
            }
        )
    return commits


def blame(
    name: str,
    path: str,
    start: int | None = None,
    end: int | None = None,
    *,
    rev: str = "HEAD",
) -> list[dict[str, Any]]:
    """
    Return the commit that last changed each line of `path` in mounted repository
    `name`, as of `rev`.

        line = maji.blame("api", "src/billing/invoice.py", 120)[0]
        print(line["sha"][:10], line["date"], line["subject"])

    Args:
        name: the mount's name, as `mounts()` gives it
        path: the file, relative to the repository's top
        start, end: the first and last line, from 1; `end` defaults to `start`. With
            neither, every line.
        rev: a branch, tag or commit

    Returns:
        One entry per line: `line` (its number in `rev`), `text`, `sha`, `author`,
        `email`, `date` (the author's, ISO 8601) and `subject`
    """
    # `git blame` doesn't take `--end-of-options` as `git log` does: given
    # `--end-of-options HEAD -- app.py`, it reports `app.py` as a bad revision. So a
    # revision that reads as an option is refused here instead.
    #
    if rev.startswith("-"):
        raise StubError("invalid_argument", f"{rev!r} isn't a revision", {})
    args = ["blame", "--line-porcelain", "--no-textconv"]
    if start is not None:
        args.append(f"-L{int(start)},{int(end if end is not None else start)}")
    args += [rev, "--", path]

    lines: list[dict[str, Any]] = []
    entry: dict[str, Any] = {}
    for raw in git(name, *args).split("\n"):
        if raw.startswith("\t"):
            entry["text"] = raw[1:]
            lines.append(entry)
            entry = {}
        elif not entry:
            if raw:
                sha, _, final = raw.split(" ")[:3]
                entry = {"line": int(final), "sha": sha}
        else:
            key, _, value = raw.partition(" ")
            if key == "author":
                entry["author"] = value
            elif key == "author-mail":
                entry["email"] = value.strip("<>")
            elif key == "author-time":
                entry["_time"] = int(value)
            elif key == "author-tz":
                entry["date"] = _iso(entry.pop("_time"), value)
            elif key == "summary":
                entry["subject"] = value
    return lines


def sqlite(name: str, path: str | None = None) -> sqlite3.Connection:
    """
    Open a mounted SQLite database, read-only.

        db = maji.sqlite("captures", "worker.db")
        print(db.execute("SELECT COUNT(*) FROM logs").fetchone()[0])

    A database in WAL mode keeps its newest rows in a `-wal` file beside it. When
    that file is there, as after a writer stopped without closing, the database is
    opened with `mode=ro`, which reads it. When it isn't, after a clean close, the
    database is opened as immutable, since `mode=ro` would need to create a file on
    the read-only mount.

    A database still being written isn't read reliably through a mount: SQLite needs
    every reader and writer on one machine. Read a snapshot of it instead, where its
    writer publishes them: mount the folder of snapshots and open the link to the
    newest. A snapshot never changes, so it opens as immutable, and the connection
    keeps reading the one it opened; open the link again for newer rows.

    Args:
        name: the mount's name: the database itself, or the folder holding it
        path: the database's path inside a mounted folder

    Raises:
        StubError: `not_found` when there is no such mount or file
    """
    database = _mounted(name) / path if path else _mounted(name)
    if not database.is_file():
        raise StubError("not_found", f"no database at {database}", {})
    mode = "mode=ro" if Path(f"{database}-wal").exists() else "immutable=1"
    connection = sqlite3.connect(f"{database.as_uri()}?{mode}", uri=True)
    connection.execute("PRAGMA query_only = 1")
    return connection


def _iso(epoch: int, tz: str) -> str:
    """Render git's seconds and `+hhmm` offset as an ISO 8601 time."""
    sign = -1 if tz.startswith("-") else 1
    offset = timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])) * sign
    return datetime.fromtimestamp(epoch, UTC).astimezone(timezone(offset)).isoformat()


###############################################################################
#
class Namespace:
    """
    The stubs under one prefix, as attributes: `sentry.issues(...)`.

    An attribute that isn't a stub still makes a call, and the broker answers
    `not_found`, so a misspelt name fails loudly rather than returning nothing.
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        full = f"{self._prefix}.{name}"

        def stub(**args: Any) -> Any:
            return call(full, **args)

        stub.__name__ = name
        stub.__qualname__ = full
        return stub

    def __repr__(self) -> str:
        return f"<maji namespace {self._prefix}>"


def namespaces() -> dict[str, Namespace]:
    """
    Return a `Namespace` for each namespace this run may call.

    The built-in `maji` namespace is left out: this module answers for it itself (see
    `__getattr__`), so a script's `maji` stays this module, with `call` and
    `StubError`. So is `skills`, which is `skills` below.
    """
    names = {entry["name"].split(".", 1)[0] for entry in describe()} - {
        "maji",
        "skills",
    }
    return {name: Namespace(name) for name in sorted(names)}


###############################################################################
#
# A skill may call other skills, which may call others, up to this deep.
#
MAX_SKILL_DEPTH = 8

# Each skill is loaded once a run, the first time it is called. A skill's constants
# may call another skill as it loads, so the lock lets the thread holding it in again.
#
_loading = threading.RLock()
_loaded: dict[str, Callable[..., Any]] = {}
_globals: dict[str, Any] = {}


class Skills:
    """
    The skills this run may call, as functions: `skills.culprits(project="api")`.

    A skill's code comes from the broker the first time it is called, after the same
    checks a stub call gets: the run's principal may call `skills.<name>`, and the
    library has a version this run may load, which is the one the user promoted or
    one this session saved. It then runs in this process with this script's stubs,
    so it reaches nothing the script couldn't.

    A skill that fails raises as any function would, and its traceback shows its own
    lines, under the file name `<skill publisher/pack/name@version>`.
    """

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)

        def skill(*args: Any, **kwargs: Any) -> Any:
            return _call_skill(name, args, kwargs)

        skill.__name__ = name
        skill.__qualname__ = f"skills.{name}"
        return skill

    def __repr__(self) -> str:
        return "<maji skills>"


skills = Skills()


def _call_skill(name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    depth: int = getattr(_local, "skill_depth", 0)
    if depth >= MAX_SKILL_DEPTH:
        raise StubError(
            "invalid_argument",
            f"skills called each other more than {MAX_SKILL_DEPTH} deep",
            {},
        )
    function = _skill(name)
    _local.skill_depth = depth + 1
    try:
        return function(*args, **kwargs)
    finally:
        _local.skill_depth = depth


def _skill(name: str) -> Callable[..., Any]:
    """
    Load a skill's code, once a run, as a module of its own whose globals are those
    a script has.

    Raises:
        StubError: the broker refused it, as `permission_denied` or `not_found`
    """
    with _loading:
        if name in _loaded:
            return _loaded[name]
        found = _connection().request("load_skill", {"name": name})
        source, filename = str(found["source"]), str(found["filename"])
        linecache.cache[filename] = (
            len(source),
            None,
            source.splitlines(keepends=True),
            filename,
        )
        if not _globals:
            _globals.update(namespaces())
            _globals.update(maji=sys.modules[__name__], skills=skills)
        module = types.ModuleType(f"skills.{name}")
        module.__dict__.update(_globals)
        exec(compile(source, filename, "exec"), module.__dict__)
        function: Callable[..., Any] = module.__dict__[found["entry"]]
        _loaded[name] = function
        return function


def __getattr__(name: str) -> Any:
    """
    Make the built-in `maji` stubs attributes of this module: `maji.whoami()`.

    Python calls a module's `__getattr__` only for names the module doesn't define, so
    `call`, `describe` and `StubError` are never forwarded.
    """
    if name.startswith("_"):
        raise AttributeError(name)
    return getattr(Namespace("maji"), name)
