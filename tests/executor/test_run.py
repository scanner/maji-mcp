"""
Tests for the runtime image's entry point (`src/maji_mcp/executor/run.py`): what it
tells the broker about the libraries a script used, and that telling it never changes
how the script's run ends.

The functions are tested on this machine, with modules written under a folder named
`site-packages` standing in for installed libraries. The end-to-end tests run `run.py`
as the image does, with a script on standard input. The Docker test in
`tests/docker/test_libraries.py` runs it in a real executor.
"""

# system imports
import contextlib
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

# 3rd party imports
import pytest
from pytest_check import check

# project imports
from tests.support.paths import MAJI_MODULE, RUN_MODULE


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """
    `run.py` as a module, importing a fresh copy of the stub client as `maji`, as it
    does in the image.
    """
    monkeypatch.setitem(sys.modules, "maji", _load("maji", MAJI_MODULE))
    return _load("run_under_test", RUN_MODULE)


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """
    A library installed in site-packages, `fakelib`, whose `explode()` raises a
    ValueError from inside it, and whose `Frame` class lives in `fakelib.frames`.
    """
    package = tmp_path / "site-packages" / "fakelib"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "class Frame:\n"
        "    __module__ = 'fakelib.frames'\n"
        "\n"
        "def explode():\n"
        "    raise ValueError('inside the library')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path / "site-packages"))
    monkeypatch.delitem(sys.modules, "fakelib", raising=False)
    return importlib.import_module("fakelib")


def _raised(action: Callable[[], Any]) -> BaseException:
    """The exception `action` raises, with its traceback."""
    try:
        action()
    except BaseException as exc:  # noqa: BLE001 - SystemExit is a case under test
        return exc
    raise AssertionError("nothing was raised")


def test_a_failure_is_put_down_to_the_library_it_was_raised_in(
    run: ModuleType, installed: ModuleType
) -> None:
    """
    GIVEN: A script that calls a library, which raises from its own code
     WHEN: run.py describes how the script failed
     THEN: it names the exception's class and the library, from the innermost frame in
           site-packages
    """
    error = _raised(installed.explode)
    assert run.failure(error) == {"error": "ValueError", "in": "fakelib"}


def _own_key_error(installed: ModuleType) -> Callable[[], Any]:
    found: dict[str, int] = {}
    return lambda: found["missing"]


def _missing_attribute(installed: ModuleType) -> Callable[[], Any]:
    return lambda: installed.Frame().groupby_nothing


def _compiled_error(installed: ModuleType) -> Callable[[], Any]:
    # A library's compiled code raises its own exception class, with no Python frame
    # of the library's in the traceback: a class whose module is `_fakelib` stands in.
    #
    error = type("CatalogException", (Exception,), {"__module__": "_fakelib"})

    def raising() -> None:
        raise error("no such table")

    return raising


def _exiting(status: int | None) -> Callable[[ModuleType], Callable[[], Any]]:
    return lambda installed: lambda: sys.exit(status)


@pytest.mark.parametrize(
    ("action", "described"),
    [
        (_own_key_error, {"error": "KeyError", "in": None}),
        (_missing_attribute, {"error": "AttributeError", "in": "fakelib"}),
        (_compiled_error, {"error": "CatalogException", "in": "_fakelib"}),
        (_exiting(3), {"error": "SystemExit", "in": None}),
        (_exiting(0), None),
        (_exiting(None), None),
    ],
)
def test_a_failure_is_described_by_its_class_and_where_it_came_from(
    run: ModuleType,
    installed: ModuleType,
    action: Callable[[ModuleType], Callable[[], Any]],
    described: dict[str, Any] | None,
) -> None:
    """
    GIVEN: A script that ends with an exception raised in its own code
     WHEN: run.py describes how the script failed
     THEN: an error of the script's own names no library; an AttributeError names the
           library of the object that lacked the attribute; an exception whose class
           a library defines names that library; and a script that exits with status
           0 didn't fail
    """
    assert run.failure(_raised(action(installed))) == described


def test_help_works_as_before_and_counts_what_it_was_asked_about(
    run: ModuleType, installed: ModuleType
) -> None:
    """
    GIVEN: A script that calls help() on a library's module, its class, an instance,
           a function by its dotted name, and with no argument at all
     WHEN: run.py's help stands in for Python's
     THEN: each call reaches Python's help with the same arguments and returns what it
           returns, and is shown as Python's help is
      AND: the calls are counted by the top-level package each was about
    """
    calls: list[tuple[Any, ...]] = []

    class Helper:
        def __call__(self, *args: Any) -> str:
            calls.append(args)
            return "documented"

        def __repr__(self) -> str:
            return "Type help() for interactive help."

    asking = run.Asking(Helper())
    answers = [
        asking(installed),
        asking(installed.Frame),
        asking(installed.Frame()),
        asking("fakelib.explode"),
        asking(),
    ]
    with check:
        assert answers == ["documented"] * 5
    with check:
        assert len(calls) == 5 and calls[-1] == ()
    with check:
        assert repr(asking) == "Type help() for interactive help."
    with check:
        assert asking.asked == {"fakelib": 4}


def test_imported_names_each_installed_package_once(
    run: ModuleType, installed: ModuleType
) -> None:
    """
    GIVEN: A script that imported a library from site-packages, along with modules of
           the standard library
     WHEN: run.py lists what the script imported
     THEN: it names the library's top-level package once, and no module of the
           standard library
    """
    importlib.import_module("json")
    names = run.imported()
    with check:
        assert names.count("fakelib") == 1
    with check:
        assert "json" not in names and "os" not in names


def test_the_report_sends_what_the_script_used(
    run: ModuleType, installed: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    GIVEN: A script that imported a library, asked help() about it, and failed in it
     WHEN: run.py reports to the broker as the script ends
     THEN: it sends one `used` request with what was imported, what was asked about,
           and how the script failed, and waits for the broker no longer than
           REPORT_TIMEOUT_S
    """
    sent: list[tuple[str, dict[str, Any], float | None]] = []

    class Connection:
        def request(
            self, method: str, params: dict[str, Any], timeout: float | None = None
        ) -> Any:
            sent.append((method, params, timeout))
            return {}

    monkeypatch.setattr(sys.modules["maji"], "_connection", Connection)
    asking = run.Asking(lambda *args: None)
    asking(installed)
    run.report(asking, _raised(installed.explode))
    [(method, params, timeout)] = sent
    with check:
        assert method == "used"
    with check:
        assert "fakelib" in params["imported"]
    with check:
        assert params["asked"] == {"fakelib": 1}
    with check:
        assert params["failed"] == {"error": "ValueError", "in": "fakelib"}
    with check:
        assert timeout == run.REPORT_TIMEOUT_S


def test_a_report_that_fails_is_dropped(
    run: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    GIVEN: A broker the report can't reach
     WHEN: run.py reports as the script ends
     THEN: the report is dropped: nothing is raised, and nothing is printed
    """

    def unreachable() -> Any:
        raise ConnectionRefusedError("no broker")

    monkeypatch.setattr(sys.modules["maji"], "_connection", unreachable)
    run.report(run.Asking(lambda *args: None), None)
    assert capsys.readouterr() == ("", "")


@pytest.fixture
def silent_broker() -> Iterator[Path]:
    """
    A broker's call socket that answers the stub client's request for the run's stubs
    (none), and never answers anything after it, like a broker that has stopped
    responding.
    """
    # macOS limits a Unix socket's path to about 104 bytes, and pytest's tmp_path is
    # longer than that, so the socket lives in a short directory of its own.
    #
    root = Path(tempfile.mkdtemp(prefix="maji-", dir="/tmp"))
    path = root / "call.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen()
    held: list[socket.socket] = []

    def serve() -> None:
        with contextlib.suppress(OSError):
            while True:
                connection, _ = server.accept()
                held.append(connection)
                request = json.loads(connection.makefile("rb").readline())
                if request["method"] == "describe":
                    reply = {"jsonrpc": "2.0", "id": request["id"], "result": []}
                    connection.sendall(json.dumps(reply).encode() + b"\n")

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield path
    finally:
        server.close()
        for connection in held:
            connection.close()
        path.unlink()
        root.rmdir()


def _run_script(code: str, sock: Path) -> subprocess.CompletedProcess[str]:
    """
    Run a script through run.py as the image does: the script on standard input, the
    run's socket in the environment.

    `run.py` imports `maji` from its own folder here, where the image has it in
    `/opt/maji`, so Python runs without `-I`, which would leave that folder out.
    """
    return subprocess.run(
        [sys.executable, str(RUN_MODULE)],
        input=code,
        capture_output=True,
        text=True,
        env={"MAJI_BROKER_SOCKET": str(sock), "MAJI_RUN_TOKEN": "t", "PATH": ""},
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize(
    ("code", "exit_code", "stdout", "last_error_line"),
    [
        ("print('found 3')", 0, "found 3\n", None),
        ("print('partial')\n{}['missing']", 1, "partial\n", "KeyError: 'missing'"),
        ("import sys\nsys.exit(4)", 4, "", None),
    ],
)
def test_a_broker_that_never_answers_doesnt_change_how_the_run_ends(
    silent_broker: Path,
    code: str,
    exit_code: int,
    stdout: str,
    last_error_line: str | None,
) -> None:
    """
    GIVEN: A broker that takes the executor's connection and never answers
     WHEN: a script ends, by finishing, by failing, or by exiting with a status
     THEN: the run ends with the script's own exit code and output, the traceback of
           a failure as Python prints it, and nothing of the report
      AND: the run ends once REPORT_TIMEOUT_S has passed, not when the broker answers
    """
    started = time.monotonic()
    result = _run_script(code, silent_broker)
    took = time.monotonic() - started
    with check:
        assert result.returncode == exit_code
    with check:
        assert result.stdout == stdout
    errors = [
        line for line in result.stderr.splitlines() if not line.startswith("maji: ")
    ]
    with check:
        assert (errors[-1] if errors else None) == last_error_line
    with check:
        assert took < 10
