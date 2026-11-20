"""
Tests for what the broker serves and records about the libraries a script can import:
their notes through read_api and search_api, and each run's report of the libraries it
used, over the broker's real sockets.
"""

# system imports
from typing import Any

# 3rd party imports
import pytest
from pytest_check import check

# project imports
from maji_mcp.broker.catalog import Catalog, Stub
from maji_mcp.common.protocol import MajiError, call_unix
from maji_mcp.common.runtime_libraries import BY_NAME, listing
from tests.support.services import Services


async def _report(services: Services, token: str, **report: Any) -> Any:
    """Send a run's report of the libraries it used, as the executor does at its end."""
    return await call_unix(services.call_sock, "used", {"token": token, **report})


def _rows(services: Services, run_id: str) -> list[tuple[Any, ...]]:
    """The run log's rows of the libraries a run used, by library."""
    return services.broker.runlog.db.execute(
        "SELECT library, imported, asked, error, raised_here FROM runtime_libraries"
        " WHERE run_id = ? ORDER BY library",
        (run_id,),
    ).fetchall()


async def test_a_runs_report_records_the_listed_libraries_it_used(
    services: Services,
) -> None:
    """
    GIVEN: A run whose script imported pandas and duckdb, asked help() about pandas
           and a tree-sitter grammar, and failed with a KeyError raised in numpy
     WHEN: the executor reports what the script used
     THEN: the run log holds a row for each listed library the run used: duckdb and
           pandas imported, pandas asked about twice, tree_sitter once
      AND: the failure counts against pandas, which numpy comes with, and every row
           carries the run's error
      AND: modules that aren't listed libraries, such as numpy itself, os and
           requests, are left out
    """
    run_id, token = await services.start_run("reader")
    await _report(
        services,
        token,
        imported=["_duckdb", "duckdb", "numpy", "pandas", "requests"],
        asked={"pandas": 2, "tree_sitter_go": 1, "os": 3},
        failed={"error": "KeyError", "in": "numpy"},
    )
    assert _rows(services, run_id) == [
        ("duckdb", 1, 0, "KeyError", 0),
        ("pandas", 1, 2, "KeyError", 1),
        ("tree_sitter", 0, 1, "KeyError", 0),
    ]


async def test_only_a_runs_first_report_counts(services: Services) -> None:
    """
    GIVEN: A run that has reported the libraries it used
     WHEN: a second report arrives with the run's token, as a script could send
     THEN: it is answered, and changes nothing in the run log
    """
    run_id, token = await services.start_run("reader")
    await _report(services, token, imported=["yaml"], asked={}, failed=None)
    await _report(services, token, imported=["pandas"], asked={"pandas": 9})
    assert _rows(services, run_id) == [("yaml", 1, 0, None, 0)]


@pytest.mark.parametrize(
    ("report", "rows"),
    [
        (
            {"imported": "pandas", "asked": ["pandas"], "failed": "KeyError"},
            [],
        ),
        (
            {"imported": [7, None, "yaml"], "asked": {"yaml": "twice", "git": -1}},
            [("yaml", 1, 0, None, 0)],
        ),
        (
            {
                "imported": ["yaml"],
                "failed": {"error": "Key Error; drop", "in": "yaml"},
            },
            [("yaml", 1, 0, None, 0)],
        ),
        (
            {"imported": ["yaml"], "failed": {"error": "KeyError", "in": ["yaml"]}},
            [("yaml", 1, 0, "KeyError", 0)],
        ),
    ],
)
async def test_a_malformed_report_records_only_what_can_be_read(
    services: Services, report: dict[str, Any], rows: list[tuple[Any, ...]]
) -> None:
    """
    GIVEN: A run whose report of the libraries it used is malformed, in part or whole
     WHEN: the broker reads it
     THEN: it keeps only names of listed libraries, positive counts, and an error
           named like a Python class; the rest is left out, without failing the run
    """
    run_id, token = await services.start_run("reader")
    await _report(services, token, **report)
    assert _rows(services, run_id) == rows


@pytest.mark.parametrize(
    ("failed", "recorded"),
    [
        ({"error": "ParserError", "in": "yaml"}, ("ParserError", 1)),
        ({"error": "TypeError", "in": None}, ("TypeError", 0)),
        ({"error": "StubError", "in": "maji"}, ("StubError", 0)),
        ({"error": "AcmeBillingSyncFailed", "in": None}, ("other", 0)),
    ],
)
async def test_a_failure_is_recorded_by_a_class_python_or_a_library_names(
    services: Services, failed: dict[str, Any], recorded: tuple[str, int]
) -> None:
    """
    GIVEN: A run that imported yaml and failed
     WHEN: the executor reports how it failed
     THEN: a class a runtime library raised, one of Python's own, or the stub
           client's StubError is recorded by name
      AND: a class the script defined itself, which can carry a project's names, is
           recorded as `other`
    """
    run_id, token = await services.start_run("reader")
    await _report(services, token, imported=["yaml"], failed=failed)
    [(_, _, _, error, raised_here)] = _rows(services, run_id)
    assert (error, raised_here) == recorded


async def test_a_report_is_neither_a_call_nor_counted_against_the_rate_limit(
    services: Services,
) -> None:
    """
    GIVEN: A principal allowed 3 calls a minute, and a run of its that reports the
           libraries it used 5 times
     WHEN: the run finishes
     THEN: its result names no calls, and the run log holds none
    """
    run_id, token = await services.start_run("reader")
    for _ in range(5):
        await _report(services, token, imported=["yaml"])
    finished = await services.control(
        "finish_run", run_id=run_id, exit_code=0, duration_ms=1, output_bytes=0
    )
    with check:
        assert finished["called"] == []
    with check:
        assert services.broker.runlog.calls(run_id) == []


async def test_a_report_without_a_live_token_is_refused(services: Services) -> None:
    """
    GIVEN: A token that belongs to no live run
     WHEN: a report of libraries arrives with it
     THEN: the broker refuses it as unauthenticated, and records nothing
    """
    with pytest.raises(MajiError) as refused:
        await _report(services, "not-a-token", imported=["yaml"])
    with check:
        assert refused.value.code == "unauthenticated"
    with check:
        assert services.broker.runlog.db.execute(
            "SELECT COUNT(*) FROM runtime_libraries"
        ).fetchone() == (0,)


async def test_read_api_serves_the_libraries_and_records_each_read(
    services: Services,
) -> None:
    """
    GIVEN: An agent asking about the libraries a script can import
     WHEN: it reads read_api("python"), then read_api("python.pandas"), then the notes
           of a library that isn't there
     THEN: it gets the list, then pandas's notes, then not_found naming
           read_api("python")
      AND: the run log records the two reads that found something, by the principal
    """
    found = [
        await services.control("read_api", principal="reader", name="python"),
        await services.control("read_api", principal="reader", name="python.pandas"),
    ]
    with pytest.raises(MajiError) as missing:
        await services.control("read_api", principal="reader", name="python.numpy")
    with check:
        assert found == [listing(), BY_NAME["pandas"].described()]
    with check:
        assert missing.value.code == "not_found"
    with check:
        assert 'read_api("python")' in missing.value.message
    with check:
        assert services.broker.runlog.db.execute(
            "SELECT principal, name FROM lookups ORDER BY rowid"
        ).fetchall() == [("reader", "python"), ("reader", "python.pandas")]


async def test_search_api_finds_a_library_after_the_stubs(services: Services) -> None:
    """
    GIVEN: An agent searching the API for a word only a library's summary has
     WHEN: it searches
     THEN: the library comes back, shown as an import
    """
    found = await services.control("search_api", principal="reader", query="yaml")
    assert [(f["name"], f["signature"]) for f in found] == [
        ("python.yaml", "import yaml")
    ]


def test_no_stub_may_take_the_libraries_name() -> None:
    """
    GIVEN: A stub in a namespace called `python`, such as a server's tool could make
     WHEN: it is added to the catalog
     THEN: the catalog refuses it, since read_api("python") names the libraries
    """

    async def handler(args: dict[str, Any], ctx: Any) -> None:
        return None

    with pytest.raises(ValueError, match="`python` names the runtime's libraries"):
        Catalog().add("1.0", Stub("python.run", "Run Python.", {}, "None", handler))
