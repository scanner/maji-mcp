"""
The stub catalog: every call a script can make, with its signature and its handler.

A stub is the broker's typed front for one operation of one service, named
`<namespace>.<operation>`, such as `sentry.issues`. The catalog checks a call's
arguments against the stub's parameters before the handler runs, so a misspelt or
missing argument comes back as `invalid_argument` rather than as a confusing failure
from the service. The same definitions render the documentation `read_api` shows an
agent, as Python signatures.

Stub names are stable, and the catalog is versioned per namespace
(`docs/design/planned/sharing.md` decision 3), so a skill written against `sentry` 1.x
keeps working until a breaking change makes it 2.0.
"""

# system imports
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# project imports
from maji_mcp.common.protocol import MajiError

REQUIRED = object()

# The parameter types a stub may declare, with the Python types that satisfy each.
# `bool` is checked before `int`, because a bool is also an int in Python.
#
TYPES: dict[str, tuple[type, ...]] = {
    "str": (str,),
    "int": (int,),
    "bool": (bool,),
    "float": (int, float),
    "list[str]": (list,),
    "dict": (dict,),
    "Any": (object,),
}


@dataclass(frozen=True)
class CallContext:
    """
    Who is calling: the run's principal, id and session, as the broker recorded them.

    A handler passes the principal and run id on to an MCP server in `_meta`, as
    `maji-mcp/principal` and `maji-mcp/run_id`, for its logs
    (`docs/design/server-contract.md` A8). The session scopes `maji.save` and
    `maji.load`. None of them come from the script.
    """

    principal: str
    run_id: str
    session: str | None = None


Handler = Callable[[dict[str, Any], CallContext], Awaitable[Any]]


###############################################################################
#
@dataclass(frozen=True)
class Param:
    """One parameter of a stub."""

    type: str
    description: str
    default: Any = REQUIRED

    def __post_init__(self) -> None:
        if self.type not in TYPES:
            raise ValueError(f"unknown parameter type {self.type!r}")

    @property
    def required(self) -> bool:
        return self.default is REQUIRED


@dataclass(frozen=True)
class Stub:
    """
    One operation a script can call.

    Args:
        name: `<namespace>.<operation>`
        summary: one line for listings
        params: the parameters, in signature order
        returns: the return type as Python would write it, then what it holds
        handler: called with the checked arguments, defaults filled in, and the
            call's context
        writes: True for an operation that changes something. Writes will become
            changesets (`docs/design/planned/writes.md`); until then the broker
            refuses them.
        notes: anything else an agent should know before calling it
    """

    name: str
    summary: str
    params: dict[str, Param]
    returns: str
    handler: Handler
    writes: bool = False
    notes: str = ""

    @property
    def namespace(self) -> str:
        return self.name.split(".", 1)[0]

    def signature(self) -> str:
        """Return the stub as a Python signature, with keyword-only parameters."""
        parts = []
        for pname, param in self.params.items():
            annotation = param.type
            if param.required:
                parts.append(f"{pname}: {annotation}")
            else:
                parts.append(f"{pname}: {annotation} = {param.default!r}")
        args = ", ".join(parts)
        returns = self.returns.split(":", 1)[0].strip()
        return f"{self.name}({'*, ' if args else ''}{args}) -> {returns}"

    def doc(self) -> str:
        """Return the full documentation `read_api` shows."""
        lines = [self.signature(), "", self.summary]
        if self.params:
            lines += ["", "Args:"]
            lines += [f"    {n}: {p.description}" for n, p in self.params.items()]
        lines += ["", "Returns:", f"    {self.returns}"]
        if self.writes:
            lines += ["", "Writes: becomes a changeset that waits for approval."]
        if self.notes:
            lines += ["", self.notes]
        return "\n".join(lines)

    def bind(self, args: dict[str, Any]) -> dict[str, Any]:
        """
        Check a call's arguments against the parameters, and fill in defaults.

        Raises:
            MajiError: `invalid_argument`, naming the unknown, missing or mistyped
                argument
        """
        unknown = set(args) - set(self.params)
        if unknown:
            raise MajiError(
                "invalid_argument",
                f"{self.name} has no parameter {sorted(unknown)[0]!r}",
                {"signature": self.signature()},
            )
        bound = {}
        for pname, param in self.params.items():
            if pname not in args:
                if param.required:
                    raise MajiError(
                        "invalid_argument",
                        f"{self.name} needs {pname!r}",
                        {"signature": self.signature()},
                    )
                bound[pname] = param.default
                continue
            value = args[pname]
            allowed = TYPES[param.type]
            wrong_bool = isinstance(value, bool) and bool not in allowed
            if value is not None and (wrong_bool or not isinstance(value, allowed)):
                raise MajiError(
                    "invalid_argument",
                    f"{self.name}: {pname!r} must be {param.type}",
                    {"signature": self.signature()},
                )
            bound[pname] = value
        return bound


###############################################################################
#
@dataclass
class Catalog:
    """
    Every stub the broker serves, and each namespace's version.

    Args:
        stubs: by full name
        versions: each namespace's version, such as `{"sentry": "1.0"}`
    """

    stubs: dict[str, Stub] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)

    def add(self, version: str, *stubs: Stub) -> None:
        """
        Register stubs, all in one namespace, at that namespace's version.

        Raises:
            ValueError: a stub is already registered, or names the namespace
                `python`, which read_api keeps for the libraries a script can import
        """
        for stub in stubs:
            if stub.name in self.stubs:
                raise ValueError(f"stub {stub.name} registered twice")
            if stub.namespace == "python":
                raise ValueError("`python` names the runtime's libraries, not stubs")
            self.stubs[stub.name] = stub
            self.versions[stub.namespace] = version

    def get(self, name: str, allowed: Callable[[str], bool]) -> Stub:
        """
        Return the stub of that name.

        Args:
            name: the stub's full name, such as `sentry.issues`
            allowed: says whether the caller may use a stub, by its name

        Raises:
            MajiError: `not_found`, listing the stubs of the name's namespace that
                the caller may use, so a misspelt name is put right without naming
                a stub the caller's `describe` wouldn't show
        """
        if name in self.stubs:
            return self.stubs[name]
        namespace = name.split(".", 1)[0]
        known = sorted(
            n for n in self.stubs if n.startswith(namespace + ".") and allowed(n)
        )
        raise MajiError("not_found", f"no stub {name!r}", {"available": known})

    def visible(self, allowed: Callable[[str], bool]) -> list[Stub]:
        """Return the stubs a caller may use, by name."""
        return [s for n, s in sorted(self.stubs.items()) if allowed(n)]

    def search(self, query: str, allowed: Callable[[str], bool]) -> list[Stub]:
        """
        Return the visible stubs whose name, summary or parameters mention every word
        of the query, case-insensitively.
        """
        words = query.lower().split()
        found = []
        for stub in self.visible(allowed):
            text = " ".join(
                [stub.name, stub.summary, stub.returns, *stub.params]
            ).lower()
            if all(word in text for word in words):
                found.append(stub)
        return found
