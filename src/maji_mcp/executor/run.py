"""
The entry point of the Python runtime image: run the script sent on standard input.

The runner sends the agent's script on stdin and closes it. This reads it, makes each
stub namespace the run may call available as a global (`sentry`, and so on), along with
the `maji` module itself and the run's skills (`skills`), and runs the script as
`__main__`.

When the script ends, this tells the broker which runtime libraries it used: the
top-level modules it imported from site-packages, what it called `help()` on, and, if
it failed, the exception's class and the library it came from. The broker keeps only
the names of runtime libraries (`maji_mcp.common.runtime_libraries`), in the run log,
which shows whether agents need more than the libraries' notes. Nothing the script
read, printed or passed is sent.

Standard library only, like the stub client: the runtime libraries
(`images/python-runtime/requirements.in`) are for scripts.
"""

# system imports
import builtins
import collections
import sys
import types
from typing import Any

sys.path.insert(0, "/opt/maji")

# project imports
import maji  # noqa: E402

# How long the report of what a script used may hold up the end of a run.
#
REPORT_TIMEOUT_S = 2.0


def _package(name: object) -> str | None:
    """The top-level package of a dotted module name, or None."""
    if not isinstance(name, str) or not name:
        return None
    return name.split(".", 1)[0]


def _package_of(obj: object) -> str | None:
    """
    The top-level package an object comes from: a module's own, a class's or a
    function's module, an instance's class's module, or the module a string names,
    as in `help("yaml.safe_load")`.
    """
    if isinstance(obj, str):
        return _package(obj)
    if isinstance(obj, types.ModuleType):
        return _package(obj.__name__)
    found = getattr(obj, "__module__", None)
    if not isinstance(found, str):
        found = type(obj).__module__
    return _package(found)


def _in_site_packages(filename: str) -> str | None:
    """The top-level package a file in site-packages belongs to, or None."""
    _, marker, rest = filename.partition("/site-packages/")
    if not marker:
        return None
    return _package(rest.split("/", 1)[0].removesuffix(".py"))


class Asking:
    """
    `help`, counting what a script asks it about. It works as `help` does, and is
    shown as `help` is.
    """

    def __init__(self, helper: Any) -> None:
        self.helper = helper
        self.asked: collections.Counter[str] = collections.Counter()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if args:
            package = _package_of(args[0])
            if package:
                self.asked[package] += 1
        return self.helper(*args, **kwargs)

    def __repr__(self) -> str:
        return repr(self.helper)


def failure(error: BaseException | None) -> dict[str, Any] | None:
    """
    How a script failed: the exception's class, and the package it came from.

    The package is the one holding the innermost frame in site-packages, else the
    exception class's own, for one a library raises from compiled code, else, for an
    AttributeError, that of the object that lacked the attribute. None for a script
    that ended without an error, `sys.exit(0)` included.
    """
    if error is None:
        return None
    if isinstance(error, SystemExit) and error.code in (0, None):
        return None
    came_from = None
    traceback = error.__traceback__
    while traceback is not None:
        came_from = (
            _in_site_packages(traceback.tb_frame.f_code.co_filename) or came_from
        )
        traceback = traceback.tb_next
    if came_from is None:
        own = _package(type(error).__module__)
        if own != "builtins":
            came_from = own
    if came_from is None and isinstance(error, AttributeError):
        if error.obj is not None:
            came_from = _package_of(error.obj)
    return {"error": type(error).__name__, "in": came_from}


def imported() -> list[str]:
    """The top-level modules the run imported from site-packages."""
    found = set()
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None) or ""
        if "/site-packages/" in filename:
            found.add(name.split(".", 1)[0])
    return sorted(found)


def report(asking: Asking, error: BaseException | None) -> None:
    """
    Tell the broker which libraries the script used. A report that fails is dropped:
    it never prints, raises or changes how the run ends.
    """
    try:
        maji._connection().request(
            "used",
            {
                "imported": imported(),
                "asked": dict(asking.asked),
                "failed": failure(error),
            },
            timeout=REPORT_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001, S110
        pass


def main() -> None:
    source = sys.stdin.read()
    namespace: dict[str, object] = {
        "__name__": "__main__",
        "maji": maji,
        "skills": maji.skills,
    }
    try:
        namespace.update(maji.namespaces())
    except OSError as exc:
        print(f"maji: can't reach the broker: {exc}", file=sys.stderr)
    except maji.StubError as exc:
        print(f"maji: {exc}", file=sys.stderr)
    asking = Asking(getattr(builtins, "help", None))
    if asking.helper is not None:
        setattr(builtins, "help", asking)  # noqa: B010 - help's own type is private
    error: BaseException | None = None
    try:
        code = compile(source, "<run_python>", "exec")
        exec(code, namespace)
    except BaseException as exc:
        error = exc
        raise
    finally:
        report(asking, error)


if __name__ == "__main__":
    main()
