"""
A skill's code: the function an agent saved, taken out of the script it tested it in,
and what the broker can tell about it without running it.

An agent writes a function in a `run_python` script, calls it there to see it work,
and saves it with `save_skill`. The lines around the function that called it and
printed what it returned aren't part of the skill, so `extract` keeps only what
defines things: imports, functions, classes, constants (UPPER_CASE names) and the
module docstring, each with the comment lines just above it. Every other statement at
the top level is dropped, and the result says which lines were.

The code saved is then not quite the code that ran, so `extract` also checks that
every name the kept code reads is defined in it, built in, or one of the globals a
skill is given: `maji`, `skills` and the stub namespaces. A function that leaned on a
variable the script set is refused, naming the variable, rather than saved to fail
the first time another session calls it.

`analyse` reads the kept code for what you review before promoting it: the stubs it
calls, the modules it imports, and each place that reaches code or stubs by a name
worked out at run time, which reading the code can't follow.

Nothing here runs the code. It is read with `ast` and `symtable` only.
"""

# system imports
import ast
import builtins
import keyword
import re
import symtable
import sys
from dataclasses import dataclass
from typing import Any

# project imports
from maji_mcp.common.protocol import MajiError

# A skill's name is its function's name and the last part of its id, so it is a
# Python identifier in the style of a stub's: lowercase, digits and underscores.
#
NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")

MAX_SOURCE_BYTES = 64 * 1024

# The globals a skill runs with besides its own definitions and the builtins, as a
# script does (`executor/run.py`). The stub namespaces are added by the caller, from the
# catalog.
#
GIVEN = frozenset({"maji", "skills", "__name__", "__doc__"})

# The types a skill's parameters and result are written in, and the JSON Schema type
# each is. A skill is called with JSON-like values today, from a script, and from any
# language through the runner later (`docs/design/planned/languages.md`), so its
# signature has to say what JSON carries. `Any` says nothing.
#
SCHEMA_TYPES: dict[str, str | None] = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
    "None": "null",
    "Any": None,
}

TYPES_HELP = (
    "str, int, float, bool, list, dict, Any and None, combined with [] and |, such "
    "as list[dict] or str | None"
)

# Calls that run code or reach a name worked out at run time.
#
DYNAMIC_CALLS = frozenset(
    {"exec", "eval", "compile", "__import__", "globals", "vars", "breakpoint"}
)
DYNAMIC_MODULES = frozenset({"importlib", "ctypes"})


###############################################################################
#
@dataclass(frozen=True)
class Param:
    """
    One parameter of a skill's function.

    Args:
        name: the parameter's name
        annotation: its type, as written
        default: its default, as written, or None for a required parameter
        keyword_only: it comes after `*`
    """

    name: str
    annotation: str
    default: str | None
    keyword_only: bool

    def as_dict(self) -> dict[str, Any]:
        found: dict[str, Any] = {"name": self.name, "annotation": self.annotation}
        if self.default is not None:
            found["default"] = self.default
        if self.keyword_only:
            found["keyword_only"] = True
        return found


@dataclass(frozen=True)
class Extracted:
    """
    A skill's code, taken out of a script.

    Args:
        source: the kept code
        entry: the function a call runs
        params: its parameters, in order
        returns: its result's type, as written
        docstring: the function's docstring, or ""
        input_schema: the parameters as a JSON Schema object
        output_schema: the result as a JSON Schema
        dropped: the line ranges of the script left out, from 1, inclusive
    """

    source: str
    entry: str
    params: tuple[Param, ...]
    returns: str
    docstring: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    dropped: tuple[tuple[int, int], ...]

    def signature(self, prefix: str = "skills.") -> str:
        """The function as Python writes it: `skills.name(a: str, b: int = 2) -> list`."""
        parts: list[str] = []
        star = False
        for param in self.params:
            if param.keyword_only and not star:
                parts.append("*")
                star = True
            text = f"{param.name}: {param.annotation}"
            if param.default is not None:
                text += f" = {param.default}"
            parts.append(text)
        return f"{prefix}{self.entry}({', '.join(parts)}) -> {self.returns}"


@dataclass(frozen=True)
class Analysis:
    """
    What reading a skill's code finds.

    Args:
        calls: each stub or skill it calls by name, such as `sentry.issues`, and each
            `maji` helper it uses, sorted
        imports: the top-level modules it imports, sorted
        dynamic: each place that reaches code or a stub by a name worked out at run
            time, as `line N: what`
    """

    calls: tuple[str, ...]
    imports: tuple[str, ...]
    dynamic: tuple[str, ...]

    @property
    def stub_namespaces(self) -> tuple[str, ...]:
        """The namespaces of the stubs it calls, leaving out `maji` and `skills`."""
        return tuple(
            sorted(
                {c.split(".", 1)[0] for c in self.calls} - {"maji", "skills"},
            )
        )

    @property
    def beyond_stdlib(self) -> tuple[str, ...]:
        """The modules it imports that aren't in Python's standard library."""
        return tuple(m for m in self.imports if m not in sys.stdlib_module_names)


def check_name(name: object) -> str:
    """
    Raises:
        MajiError: `invalid_argument` for a name that isn't a skill's
    """
    if not isinstance(name, str) or not NAME.match(name) or keyword.iskeyword(name):
        raise MajiError(
            "invalid_argument",
            "a skill's name is 2 to 41 lowercase letters, digits or _, starting with "
            "a letter, and not a Python keyword",
        )
    return name


###############################################################################
#
def extract(script: str, entry: str, namespaces: frozenset[str]) -> Extracted:
    """
    Take a skill's code out of the script it was tested in.

    Args:
        script: the script, or just the skill's code
        entry: the function a call to the skill runs, which is its name
        namespaces: the stub namespaces a skill may reach, which are its globals

    Returns:
        The kept code, and what its function takes and returns

    Raises:
        MajiError: `invalid_argument` when the script doesn't parse, defines no such
            function, writes its signature in types JSON can't carry, reads a name
            nothing defines, or is too long
    """
    check_name(entry)
    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        raise MajiError(
            "invalid_argument", f"the code doesn't parse: line {exc.lineno}: {exc.msg}"
        ) from None
    lines = script.splitlines()
    kept: list[tuple[int, int]] = []
    dropped: list[tuple[int, int]] = []
    function: ast.FunctionDef | None = None
    for index, node in enumerate(tree.body):
        if not _defines(node, index):
            dropped.append((node.lineno, node.end_lineno or node.lineno))
            continue
        kept.append(_span(node, lines, kept[-1][1] if kept else 0))
        if isinstance(node, ast.FunctionDef) and node.name == entry:
            function = node
        elif isinstance(node, ast.AsyncFunctionDef) and node.name == entry:
            raise MajiError(
                "invalid_argument",
                f"{entry} is async; a skill's function is a plain def, and may call "
                "async helpers with asyncio.run",
            )
    if function is None:
        defined = sorted(
            n.name for n in tree.body if isinstance(n, ast.FunctionDef)
        ) or ["none"]
        raise MajiError(
            "invalid_argument",
            f"the code defines no function {entry} at its top level; it defines: "
            + ", ".join(defined),
        )

    source = _join(lines, kept)
    if len(source.encode()) > MAX_SOURCE_BYTES:
        raise MajiError(
            "invalid_argument",
            f"a skill's code is limited to {MAX_SOURCE_BYTES // 1024} KB",
        )
    unresolved = _unresolved(source, GIVEN | namespaces)
    if unresolved:
        raise MajiError(
            "invalid_argument",
            "the skill's code reads names it doesn't define: "
            + ", ".join(unresolved)
            + ". Lines that aren't imports, functions, classes or UPPER_CASE "
            "constants are left out of a skill, so define what it needs in one of "
            "those, or pass it in as a parameter",
        )

    params, input_schema = _params(function, _constants(tree))
    if function.returns is None:
        raise MajiError(
            "invalid_argument",
            f"say what {entry} returns, as -> followed by {TYPES_HELP}",
        )
    output_schema = _schema(function.returns, f"{entry}'s result")
    return Extracted(
        source=source,
        entry=entry,
        params=params,
        returns=ast.unparse(function.returns),
        docstring=ast.get_docstring(function) or "",
        input_schema=input_schema,
        output_schema=output_schema,
        dropped=tuple(_merge(dropped)),
    )


def _defines(node: ast.stmt, index: int) -> bool:
    """Whether a top-level statement belongs in a skill: it only defines something."""
    if isinstance(
        node,
        ast.Import
        | ast.ImportFrom
        | ast.FunctionDef
        | ast.AsyncFunctionDef
        | ast.ClassDef,
    ):
        return True
    if index == 0 and isinstance(node, ast.Expr):
        return isinstance(node.value, ast.Constant) and isinstance(
            node.value.value, str
        )
    if isinstance(node, ast.Assign):
        return all(_constant_name(t) for t in node.targets)
    if isinstance(node, ast.AnnAssign):
        return node.value is not None and _constant_name(node.target)
    return False


def _constant_name(target: ast.expr) -> bool:
    return isinstance(target, ast.Name) and target.id.isupper()


def _span(node: ast.stmt, lines: list[str], after: int) -> tuple[int, int]:
    """
    The lines a kept statement takes, from 1, with its decorators and the comment
    lines just above it, but none at or before line `after`, the end of the last kept
    statement.
    """
    decorators = getattr(node, "decorator_list", None) or []
    start = min([node.lineno, *(d.lineno for d in decorators)])
    while start - 1 > after and lines[start - 2].lstrip().startswith("#"):
        start -= 1
    return start, node.end_lineno or node.lineno


def _join(lines: list[str], spans: list[tuple[int, int]]) -> str:
    """
    Put the kept statements together, a blank line between imports and constants that
    weren't next to each other, and two before and after a function or class.
    """
    out: list[str] = []
    previous_end = 0
    for start, end in spans:
        block = lines[start - 1 : end]
        if out:
            adjacent = start == previous_end + 1
            first = block[0].lstrip()
            spaced = first.startswith(("def ", "class ", "async def ", "@", "#"))
            if spaced or out[-1].startswith((" ", "\t")):
                out += ["", ""]
            elif not adjacent:
                out.append("")
        out += block
        previous_end = end
    return "\n".join(out).rstrip() + "\n"


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Join line ranges that touch, so dropped lines read as a few ranges."""
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _unresolved(source: str, given: frozenset[str]) -> list[str]:
    """
    The names the code reads at module level that nothing defines.

    A name read in a function, or a class, and bound in none of the scopes around
    it is looked up among the module's globals and then the builtins, so it has to
    be defined at the top level, built in, or given. Annotations are evaluated only
    when something asks for them, so the names in them are left out.
    """
    top = symtable.symtable(source, "<skill>", "exec")
    defined = {
        s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported()
    }
    known = defined | given | set(dir(builtins))
    missing: set[str] = set()

    def walk(table: symtable.SymbolTable) -> None:
        if table.get_type() == symtable.SymbolTableType.ANNOTATION:
            return
        for symbol in table.get_symbols():
            name = symbol.get_name()
            if (
                symbol.is_referenced()
                and symbol.is_global()
                and not name.startswith(".")
                and name not in known
            ):
                missing.add(name)
        for child in table.get_children():
            walk(child)

    walk(top)
    return sorted(missing)


def _constants(tree: ast.Module) -> dict[str, ast.expr]:
    """The module's UPPER_CASE constants whose values are literals, by name."""
    found: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if _constant_name(target) and _literal(value):
            assert isinstance(target, ast.Name)
            found[target.id] = value
    return found


def _literal(node: ast.expr) -> bool:
    try:
        ast.literal_eval(node)
    except ValueError:
        return False
    return True


def _params(
    function: ast.FunctionDef, constants: dict[str, ast.expr]
) -> tuple[tuple[Param, ...], dict[str, Any]]:
    """
    The function's parameters, and the JSON Schema object they make.

    A default may be a literal, or an UPPER_CASE constant whose value is one, which
    the manifest records as that value.

    Raises:
        MajiError: `invalid_argument` for `*args`, `**kwargs`, positional-only
            parameters, a parameter without a type, a type JSON can't carry, or a
            default that isn't a literal
    """
    args = function.args
    if args.vararg or args.kwarg or args.posonlyargs:
        raise MajiError(
            "invalid_argument",
            f"{function.name} takes named parameters only: no *args, **kwargs or /",
        )
    positional_defaults: list[ast.expr | None] = [None] * (
        len(args.args) - len(args.defaults)
    ) + list(args.defaults)
    pairs = [(a, d, False) for a, d in zip(args.args, positional_defaults, strict=True)]
    pairs += [
        (a, d, True) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True)
    ]

    params: list[Param] = []
    properties: dict[str, Any] = {}
    required: list[str] = []
    for arg, default, keyword_only in pairs:
        if arg.annotation is None:
            raise MajiError(
                "invalid_argument",
                f"give {arg.arg} a type, one of {TYPES_HELP}",
            )
        schema = _schema(arg.annotation, f"{arg.arg}")
        written = None
        if default is not None:
            if isinstance(default, ast.Name) and default.id in constants:
                default = constants[default.id]
            if not _literal(default):
                raise MajiError(
                    "invalid_argument",
                    f"{arg.arg}'s default is {ast.unparse(default)}, which isn't a "
                    "literal; use None, and work it out in the function",
                )
            written = ast.unparse(default)
        else:
            required.append(arg.arg)
        params.append(
            Param(arg.arg, ast.unparse(arg.annotation), written, keyword_only)
        )
        properties[arg.arg] = schema
    return tuple(params), {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _schema(node: ast.expr, what: str) -> dict[str, Any]:
    """
    The JSON Schema a type annotation stands for.

    Raises:
        MajiError: `invalid_argument` for a type outside `SCHEMA_TYPES`, or `[]` on
            anything but `list` and `dict`
    """
    if isinstance(node, ast.Constant) and node.value is None:
        return {"type": "null"}
    if isinstance(node, ast.Name) and node.id in SCHEMA_TYPES:
        kind = SCHEMA_TYPES[node.id]
        return {"type": kind} if kind else {}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        options: list[dict[str, Any]] = []
        for side in (node.left, node.right):
            found = _schema(side, what)
            options += found["anyOf"] if "anyOf" in found else [found]
        return {"anyOf": options}
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        if node.value.id == "list":
            return {"type": "array", "items": _schema(node.slice, what)}
        if node.value.id == "dict" and isinstance(node.slice, ast.Tuple):
            key, value = node.slice.elts if len(node.slice.elts) == 2 else (None, None)
            if isinstance(key, ast.Name) and key.id == "str" and value is not None:
                return {"type": "object", "additionalProperties": _schema(value, what)}
    raise MajiError(
        "invalid_argument",
        f"{what} is typed {ast.unparse(node)}; a skill's types are {TYPES_HELP}, "
        "and a dict's keys are str",
    )


###############################################################################
#
def analyse(source: str, namespaces: frozenset[str]) -> Analysis:
    """
    Read code for the stubs and skills it calls, the modules it imports, and the
    places it reaches either by a name worked out at run time.

    A call is found when it is written `namespace.name(...)`, or `maji.call` with the
    stub's name written out. Anything else that could reach a stub is reported in
    `dynamic`: `maji.call` with a name the code works out, a namespace used as a
    value, such as `s = sentry`, `getattr` on a namespace, and the builtins and
    modules that run code from text.

    Args:
        source: the code, which parses
        namespaces: the stub namespaces it may reach
    """
    tree = ast.parse(source)
    reachable = namespaces | {"maji", "skills"}
    calls: set[str] = set()
    imports: set[str] = set()
    dynamic: list[str] = []
    followed: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in reachable:
                followed.add(id(node.value))
                if not (node.value.id == "maji" and node.attr == "StubError"):
                    calls.add(f"{node.value.id}.{node.attr}")
        if isinstance(node, ast.Call):
            _dynamic_call(node, reachable, calls, dynamic, followed)
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imports.add(node.module.split(".")[0])

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and node.id in reachable
            and isinstance(node.ctx, ast.Load)
            and id(node) not in followed
        ):
            dynamic.append(
                f"line {node.lineno}: {node.id} is used as a value, so calls made "
                "through it can't be followed"
            )
    for module in sorted(imports & DYNAMIC_MODULES):
        dynamic.append(f"imports {module}, which can load code by a name worked out")
    calls.discard("maji.call")
    return Analysis(
        calls=tuple(sorted(calls)),
        imports=tuple(sorted(imports)),
        dynamic=tuple(sorted(set(dynamic), key=_line_order)),
    )


def _dynamic_call(
    node: ast.Call,
    reachable: frozenset[str],
    calls: set[str],
    dynamic: list[str],
    followed: set[int],
) -> None:
    """
    Note a call that reaches code or a stub by a name worked out at run time. A
    namespace passed to `getattr` is noted here, so it is marked as followed.
    """
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "maji"
        and func.attr == "call"
    ):
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            calls.add(first.value)
        else:
            dynamic.append(
                f"line {node.lineno}: maji.call with a stub name worked out at run time"
            )
    elif isinstance(func, ast.Name) and func.id in DYNAMIC_CALLS:
        dynamic.append(
            f"line {node.lineno}: {func.id}(), which runs or reaches code by name"
        )
    elif (
        isinstance(func, ast.Name)
        and func.id == "getattr"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in reachable
    ):
        followed.add(id(node.args[0]))
        dynamic.append(
            f"line {node.lineno}: getattr on {node.args[0].id}, a stub named at run time"
        )


def _line_order(text: str) -> tuple[int, str]:
    match = re.match(r"line (\d+)", text)
    return (int(match.group(1)) if match else sys.maxsize, text)
