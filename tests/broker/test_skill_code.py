"""
Tests for a skill's code: the function taken out of the script it was tested in, and
what reading it, without running it, finds for you to review.
"""

# 3rd party imports
import pytest
from pytest_check import check

# project imports
from maji_mcp.broker.skill_code import MAX_SOURCE_BYTES, analyse, extract
from maji_mcp.common.protocol import MajiError

NAMESPACES = frozenset({"sentry", "org"})

TRIED = '''"""Frames that Sentry issues share."""
import collections
from typing import Any

TOP = 20
project_name = "api"


# Counted per frame, across every event of every issue.
@staticmethod
def unused() -> None:
    pass


def culprits(project: str, period: str = "14d", *, top: int = TOP) -> list[dict[str, Any]]:
    """The frames most of a project's issues fail in, most first."""
    counts: collections.Counter[str] = collections.Counter()
    for issue in sentry.issues(project=project, period=period):
        for event in maji.call("sentry.events", issue=issue["id"]):
            counts[event["frame"]] += 1
    return [{"frame": f, "n": n} for f, n in counts.most_common(top)]


rows = culprits(project_name)
for row in rows:
    print(row)
'''

# TRIED with `culprits` reading `project_name`, a variable the script set and a skill
# leaves out; and a `culprits` whose code is past MAX_SOURCE_BYTES.
#
LEANING = TRIED.replace("most_common(top)", "most_common(len(project_name))")
TOO_LONG = (
    "def culprits() -> int:\n"
    + ("    # " + "x" * 70 + "\n") * (MAX_SOURCE_BYTES // 70)
    + "    return 1\n"
)

# Code that calls stubs by name, through `maji.call` with the name written out, and
# through another skill, and that also reaches stubs and code by names worked out at
# run time.
#
REACHING = """import json
import importlib
import git


def run(kind: str) -> list:
    found = sentry.issues(project="api") + maji.call("sentry.events", issue="1")
    found += skills.helper(n=2)
    maji.save("found", found)
    s = sentry
    found += maji.call(f"sentry.{kind}")
    found += getattr(org, kind)()
    return eval(kind)
"""


def test_a_skill_is_the_scripts_definitions_without_the_lines_that_tried_it() -> None:
    """
    GIVEN: A script that defines a function, with imports, a constant and a comment,
           then calls it and prints what it returned
     WHEN: the function is taken out as a skill
     THEN: the skill keeps the docstring, imports, constant, functions and the
           comment above one, and leaves out the variable and the lines that called
           and printed, saying which lines those were
      AND: its signature, with the constant default written as its value, and its
           parameters and result as JSON Schema, are worked out from the function
    """
    found = extract(TRIED, "culprits", NAMESPACES)

    # What is kept of the script, and which of its lines were left out.
    #
    with check:
        assert found.source.startswith(
            '"""Frames that Sentry issues share."""\nimport '
        )
    with check:
        assert "TOP = 20" in found.source
    with check:
        assert (
            "# Counted per frame, across every event of every issue.\n@staticmethod"
            in found.source
        )
    with check:
        assert "project_name" not in found.source and "print" not in found.source
    with check:
        assert found.dropped == ((6, 6), (24, 26))

    # What the function takes and returns, as Python writes it and as JSON Schema.
    #
    with check:
        assert found.signature() == (
            "skills.culprits(project: str, period: str = '14d', *, top: int = 20)"
            " -> list[dict[str, Any]]"
        )
    with check:
        assert found.docstring == (
            "The frames most of a project's issues fail in, most first."
        )
    with check:
        assert found.input_schema == {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "period": {"type": "string"},
                "top": {"type": "integer"},
            },
            "required": ["project"],
            "additionalProperties": False,
        }
    with check:
        assert found.output_schema == {
            "type": "array",
            "items": {"type": "object", "additionalProperties": {}},
        }


def test_the_names_a_skill_is_given_need_no_definition() -> None:
    """
    GIVEN: A function that reads only names a skill is given: its stubs' namespaces,
           `maji`, `skills` and the builtins
     WHEN: it is taken out as a skill
     THEN: it is kept whole, since none of those names needs a definition in it
    """
    given = (
        "def all_of(n: int) -> list:\n"
        "    return [org.tasks(), skills.other(), maji.load('x'), len(range(n))]\n"
    )
    assert extract(given, "all_of", NAMESPACES).source == given


@pytest.mark.parametrize(
    ("script", "says"),
    [
        pytest.param(
            "def other() -> int:\n    return 1\n",
            "defines no function culprits",
            id="no such function",
        ),
        pytest.param(
            "async def culprits() -> int:\n    return 1\n", "is async", id="async"
        ),
        pytest.param(
            "def culprits(*args: int) -> int:\n    return 1\n",
            "no *args, **kwargs",
            id="*args",
        ),
        pytest.param(
            "def culprits(a, /) -> int:\n    return 1\n",
            "no *args, **kwargs or /",
            id="a positional-only parameter",
        ),
        pytest.param(
            "def culprits(a) -> int:\n    return a\n",
            "give a a type",
            id="an untyped parameter",
        ),
        pytest.param(
            "def culprits(a: set[str]) -> int:\n    return 1\n",
            "a is typed set[str]",
            id="a type JSON can't carry",
        ),
        pytest.param(
            "def culprits(a: dict[int, str]) -> int:\n    return 1\n",
            "keys are str",
            id="a dict keyed by int",
        ),
        pytest.param(
            "def culprits(a: list = list()) -> int:\n    return 1\n",
            "isn't a literal",
            id="a default worked out at run time",
        ),
        pytest.param(
            "def culprits(a: int):\n    return a\n",
            "say what culprits returns",
            id="no result type",
        ),
        pytest.param("def culprits(:\n", "doesn't parse: line 1", id="not Python"),
        pytest.param(
            LEANING,
            "reads names it doesn't define: project_name",
            id="a name the script set: which",
        ),
        pytest.param(
            LEANING,
            "pass it in as a parameter",
            id="a name the script set: how to fix it",
        ),
        pytest.param(TOO_LONG, "limited to 64 KB", id="too long"),
    ],
)
def test_code_that_cant_be_a_skill_is_refused_saying_why(
    script: str, says: str
) -> None:
    """
    GIVEN: Code whose function a skill can't be made of: missing, async, taking
           `*args` or positional-only parameters, untyped, typed in something JSON
           can't carry, with a default worked out at run time, with no result type,
           not Python at all, reading a variable the script set before calling it,
           which a skill leaves out, or past the size a skill may be
     WHEN: it is taken out as a skill
     THEN: it is refused as an invalid argument that says what to change, rather than
           saved to fail the first time another session calls it
    """
    with pytest.raises(MajiError) as caught:
        extract(script, "culprits", NAMESPACES)
    assert caught.value.code == "invalid_argument"
    assert says in caught.value.message


def test_a_function_whose_name_isnt_a_skills_is_refused() -> None:
    """
    GIVEN: A function named with a capital, which a skill's name can't have
     WHEN: it is taken out as a skill
     THEN: it is refused as an invalid argument, saying what a name may be
    """
    with pytest.raises(MajiError) as caught:
        extract("def Big() -> int:\n    return 1\n", "Big", NAMESPACES)
    assert caught.value.code == "invalid_argument"
    assert "a skill's name is 2 to 41 lowercase" in caught.value.message


def test_reading_the_code_finds_the_stubs_it_calls_and_flags_what_it_cant_follow() -> (
    None
):
    """
    GIVEN: Code that calls stubs by name, through `maji.call` with the name written
           out, and through another skill, and that also reaches stubs and code by
           names worked out at run time
     WHEN: it is read
     THEN: each stub, skill and `maji` helper called by name is found, the modules it
           imports are listed, with those beyond the standard library picked out
      AND: each place it can't follow is flagged with its line: `maji.call` with a
           worked-out name, a namespace used as a value, `getattr` on a namespace,
           `eval`, and importing `importlib`
    """
    found = analyse(REACHING, NAMESPACES)

    # What it calls by name, and what it imports.
    #
    with check:
        assert found.calls == (
            "maji.save",
            "sentry.events",
            "sentry.issues",
            "skills.helper",
        )
    with check:
        assert found.stub_namespaces == ("sentry",)
    with check:
        assert found.imports == ("git", "importlib", "json")
    with check:
        assert found.beyond_stdlib == ("git",)

    # Each place it reaches a stub or code by a name worked out at run time.
    #
    with check:
        assert found.dynamic == (
            "line 10: sentry is used as a value, so calls made through it can't be followed",
            "line 11: maji.call with a stub name worked out at run time",
            "line 12: getattr on org, a stub named at run time",
            "line 13: eval(), which runs or reaches code by name",
            "imports importlib, which can load code by a name worked out",
        )


def test_code_that_names_every_stub_it_calls_flags_nothing() -> None:
    """
    GIVEN: Code whose only stub call is written out by name, inside another call
     WHEN: it is read
     THEN: nothing is flagged as reached by a name worked out at run time
    """
    named = "def f() -> int:\n    return len(sentry.issues(project='a'))\n"
    assert analyse(named, NAMESPACES).dynamic == ()
