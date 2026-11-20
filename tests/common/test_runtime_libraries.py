"""
Tests for the list of libraries a script can import (`maji_mcp.common.runtime_libraries`): that
it matches what the runtime image installs, and what agents read from it.
"""

# system imports
import re

# 3rd party imports
import pytest
from pytest_check import check

# project imports
from maji_mcp.common.runtime_libraries import BY_NAME, LIBRARIES, OWNER, listing, search
from tests.support.paths import RUNTIME_LOCK, RUNTIME_REQUIREMENTS


def _normal(name: str) -> str:
    """A distribution's name as PyPI compares it: lower case, runs of -_. as one -."""
    return re.sub(r"[-_.]+", "-", name).lower()


def test_every_library_the_image_installs_is_listed_at_its_pinned_version() -> None:
    """
    GIVEN: The libraries the runtime image installs, named in requirements.in and
           pinned in requirements.txt
     WHEN: the list agents read is compared with them
     THEN: it names each of them and no other, each at the version the lock pins, so
           agents read about exactly what a script can import
    """
    wanted = {
        _normal(line.strip())
        for line in RUNTIME_REQUIREMENTS.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    pinned = {
        _normal(name): version
        for name, version in re.findall(
            r"^([A-Za-z0-9_.-]+)==(\S+)", RUNTIME_LOCK.read_text(), re.MULTILINE
        )
    }
    listed = {
        _normal(dist): version
        for library in LIBRARIES
        for dist, version in library.pins.items()
    }
    assert set(listed) == wanted
    for dist, version in listed.items():
        with check:
            assert pinned[dist] == version, dist


def test_each_module_belongs_to_one_library() -> None:
    """
    GIVEN: The libraries, each with its own name and the other modules that belong
           to it
     WHEN: a module is looked up to find the library it belongs to
     THEN: every module names exactly one library, so a help() call or an error is
           counted against one library only
    """
    modules = [m for library in LIBRARIES for m in (library.name, *library.modules)]
    assert len(modules) == len(set(modules)) == len(OWNER)


def test_the_listing_names_every_library_and_how_to_read_more() -> None:
    """
    GIVEN: The libraries a script can import
     WHEN: an agent reads read_api("python")
     THEN: it gets one line for each library, with its distribution, version and
           summary, after a line saying how to read one library's notes
    """
    lines = listing().splitlines()
    with check:
        assert 'read_api("python.<name>")' in lines[0]
    with check:
        assert lines[2:] == [
            f"{library.name} ({dist} {version}): {library.summary}"
            for library in LIBRARIES
            for dist, version in [next(iter(library.pins.items()))]
        ]


def test_a_librarys_notes_start_with_its_name_and_every_pin() -> None:
    """
    GIVEN: A library installed as several distributions: tree-sitter and its grammars
     WHEN: an agent reads read_api("python.tree_sitter")
     THEN: the notes name every distribution with its version, then the summary and
           the notes
    """
    described = BY_NAME["tree_sitter"].described().splitlines()
    with check:
        assert described[0].startswith("tree_sitter: tree-sitter 0.")
    with check:
        assert "tree-sitter-perl" in described[0]
    with check:
        assert described[1] == BY_NAME["tree_sitter"].summary


@pytest.mark.parametrize(
    ("query", "found"),
    [
        ("yaml", ["python.yaml"]),
        ("Syntax TREES", ["python.tree_sitter"]),
        ("dataframes", ["python.duckdb", "python.pandas"]),
        ("yaml markdown", []),
        ("   ", []),
    ],
)
def test_search_finds_a_library_by_every_word_of_the_query(
    query: str, found: list[str]
) -> None:
    """
    GIVEN: The libraries, each with a name and a summary
     WHEN: an agent searches the API
     THEN: it finds the libraries whose name or summary holds every word of the query,
           in any case, each shown as an import; a query with no words finds none
    """
    results = search(query)
    with check:
        assert [r["name"] for r in results] == found
    for result in results:
        with check:
            assert result["signature"] == "import " + result["name"].split(".", 1)[1]
