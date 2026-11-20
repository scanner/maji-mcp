"""
Tests for the skill library: versions named by what they are, and which version an
agent may run, decided by state alone.
"""

# system imports
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.broker.library import (
    PROMOTED,
    REJECTED,
    WAITING,
    Library,
    Review,
    Version,
)
from maji_mcp.common.protocol import MajiError

# The code of `count`, and the same with its result changed: two more versions of it.
#
SOURCE = "def count(n: int) -> int:\n    return n\n"
RETURNS_ZERO = SOURCE.replace("n\n", "0\n")
RETURNS_MORE = SOURCE.replace("n\n", "n + 1\n")

# The library stamps a promotion with the time it is made, and compares a save's own
# time with it. KEPT is before any promotion a test makes, and LATER is after it.
#
KEPT = "2026-11-11T09:00:00.000000+00:00"
LATER = "2999-01-01T00:00:00.000000+00:00"


@pytest.fixture
def library(tmp_path: Path) -> Library:
    """An empty skill library of the publisher `me`, in a folder of its own."""
    return Library(tmp_path / "library", "me")


@pytest.fixture
def manifest() -> dict[str, Any]:
    """What `count` is: the part of a skill's manifest the library reads."""
    return {
        "id": "me/local/count",
        "name": "count",
        "entry": "count",
        "summary": "Counts.",
    }


@pytest.fixture
def saved(faker: Faker) -> Callable[..., dict[str, Any]]:
    """
    A function that returns the record of one save: who kept the version, from which
    session, and when.
    """

    def saved(
        principal: str = "claude-code", at: str = KEPT, session: str | None = None
    ) -> dict[str, Any]:
        return {"principal": principal, "session": session or faker.sha256(), "at": at}

    return saved


@pytest.fixture
def keep(
    library: Library, manifest: dict[str, Any], saved: Callable[..., dict[str, Any]]
) -> Callable[..., Version]:
    """
    A function that saves a version of `count` in the library, kept as `saved`
    records it, and returns the version.
    """

    def keep(source: str = SOURCE, **by: Any) -> Version:
        version, _ = library.save("count", source, manifest, saved(**by))
        return version

    return keep


def test_a_version_is_named_by_its_code_and_manifest_together(
    library: Library,
    manifest: dict[str, Any],
    saved: Callable[..., dict[str, Any]],
    session: str,
    other_session: str,
) -> None:
    """
    GIVEN: A skill saved, then the same code and manifest saved again from another
           session, then the same code with only its summary changed
     WHEN: each is saved
     THEN: the first two are one version saved twice, laid out under
           `<publisher>/local/<name>/<version>` with its code, manifest and SKILL.md
      AND: changing only the summary every agent reads makes a new version
    """
    reworded_manifest = {**manifest, "summary": "Counts things."}

    first, created = library.save("count", SOURCE, manifest, saved(session=session))
    again, created_again = library.save(
        "count", SOURCE, manifest, saved(session=other_session)
    )
    reworded, _ = library.save("count", SOURCE, reworded_manifest, saved())

    # The same code and manifest are one version, named by the start of its digest,
    # however many times it is saved.
    #
    with check:
        assert created and not created_again
    with check:
        assert again.version == first.version and len(first.version) == 16
    with check:
        assert first.digest.startswith("sha256:" + first.version)
    with check:
        assert reworded.version != first.version

    # The version's folder holds what was saved, and each save of it.
    #
    folder = library.root / "me" / "local" / "count" / first.version
    with check:
        assert library.skill_id("count") == "me/local/count"
    with check:
        assert (folder / "skill.py").read_text() == SOURCE
    with check:
        assert json.loads((folder / "manifest.json").read_text()) == manifest
    with check:
        assert (folder / "SKILL.md").read_text().startswith("# count\n\nCounts.\n")
    with check:
        assert [s["session"] for s in library.saves("count", first.version)] == [
            session,
            other_session,
        ]


def test_a_kept_version_runs_for_its_agent_in_any_session(
    library: Library, keep: Callable[..., Version]
) -> None:
    """
    GIVEN: A version one agent kept in one of its sessions, not promoted
     WHEN: that agent asks for it from another session, and another agent asks
     THEN: the agent that kept it gets it, from any session; the other is told there
           is no skill it may run, and which ones it may
    """
    version = keep()

    assert library.loadable("count", "claude-code") == (version, "kept")
    with pytest.raises(MajiError) as caught:
        library.loadable("count", "advisor")
    assert caught.value.code == "not_found"
    assert caught.value.data == {"available": []}
    assert library.visible_names("claude-code") == ["skills.count"]


def test_a_promoted_version_runs_everywhere_unless_an_agent_kept_a_newer_one(
    library: Library, keep: Callable[..., Version]
) -> None:
    """
    GIVEN: A version one agent kept before another version was promoted, and a newer
           version another agent kept after
     WHEN: each agent asks for the skill
     THEN: the agent with a version kept after the promotion gets its own, to try it;
           the agent whose version came before gets the promoted one, as does every
           other agent
    """
    keep(RETURNS_ZERO)
    first = keep(principal="advisor")
    library.promote("count", first, {"item": "i-1", "decision": "d-1"})
    newer = keep(RETURNS_MORE, principal="helper", at=LATER)

    with check:
        assert library.loadable("count", "helper") == (newer, "kept")
    with check:
        assert library.loadable("count", "claude-code") == (first, "promoted")
    with check:
        assert library.loadable("count", "advisor") == (first, "promoted")
    with check:
        assert library.loadable("count", "anyone") == (first, "promoted")


def test_a_kept_version_stays_its_agents_whatever_became_of_its_promotion(
    library: Library, keep: Callable[..., Version]
) -> None:
    """
    GIVEN: A kept version whose promotion you declined
     WHEN: the agent that kept it asks for it
     THEN: it still gets it: declining to promote a skill declines to share it
    """
    version = keep()
    library.set_review(
        "count", version.version, Review("i-1", REJECTED, version.digest)
    )
    assert library.loadable("count", "claude-code") == (version, "kept")


def test_a_promoted_version_whose_files_changed_runs_nowhere(
    library: Library, keep: Callable[..., Version]
) -> None:
    """
    GIVEN: A promoted version whose code is then changed on disk
     WHEN: any agent asks for it, the one that kept it included
     THEN: it isn't offered: its files no longer hash to what was promoted
    """
    version = keep()
    library.promote("count", version, {"item": "i-1"})
    path = library.root / "me" / "local" / "count" / version.version / "skill.py"
    path.write_text(SOURCE + "import os\n")

    assert library.version("count", version.version) is None
    assert library.promoted("count") is None
    for principal in ("advisor", "claude-code"):
        with pytest.raises(MajiError):
            library.loadable("count", principal)


def test_a_demoted_version_never_runs_again(
    library: Library, keep: Callable[..., Version]
) -> None:
    """
    GIVEN: A promoted version, kept by one agent
     WHEN: it is demoted
     THEN: no session gets it, the agent that kept it included, which is told it
           was demoted; its revocation is recorded with the decision, and even a
           promotion record put back by hand doesn't revive it
    """
    version = keep()
    library.promote("count", version, {"item": "i-1"})

    demotion = {"decision": "d-2", "at": "2026-11-11T10:00:00Z", "note": "Off by one"}
    assert library.demote("count", demotion) == version
    assert library.promoted("count") is None
    assert library.revoked("count")[0]["decision"] == "d-2"
    with pytest.raises(MajiError) as caught:
        library.loadable("count", "claude-code")
    assert caught.value.message.startswith(
        f"the user demoted skills.count, version {version.version}, on 2026-11-11: "
        "Off by one. No agent runs that version now"
    )
    library.promote("count", version, {"item": "i-1"})
    assert library.promoted("count") is None


def test_waiting_items_are_found_by_item(
    library: Library, keep: Callable[..., Version]
) -> None:
    """
    GIVEN: Two versions, one waiting on an item and one promoted
     WHEN: the library is asked which items wait, and which version an item is for
     THEN: only the waiting item is listed, and each item leads to its version
    """
    one, two = keep(), keep(RETURNS_ZERO)
    library.set_review("count", one.version, Review("i-1", WAITING, one.digest))
    library.set_review("count", two.version, Review("i-2", PROMOTED, two.digest))

    assert library.waiting() == {"i-1": ("count", one.version)}
    assert library.by_item("i-2") == ("count", two.version)
    assert library.by_item("i-3") is None


@pytest.mark.parametrize(
    "name",
    ["../etc", "Count", "x", "count/../../x", "import"],
    ids=[
        "a path out",
        "a capital",
        "one letter",
        "a path through",
        "a Python keyword",
    ],
)
def test_a_name_that_isnt_a_skills_reaches_no_file(
    library: Library,
    manifest: dict[str, Any],
    saved: Callable[..., dict[str, Any]],
    name: str,
) -> None:
    """
    GIVEN: A name with a path in it, capitals, one letter, or a Python keyword
     WHEN: the library is asked to save a skill by it
     THEN: it is refused before any path is made from it, and nothing is written
    """
    with pytest.raises(MajiError) as caught:
        library.save(name, SOURCE, manifest, saved())
    assert caught.value.code == "invalid_argument"
    assert not library.root.exists()


def test_a_version_that_isnt_one_reaches_no_file(library: Library) -> None:
    """
    GIVEN: A version that is a path out of the library, where a version is 16 hex
           digits
     WHEN: the library is asked for that version of a skill
     THEN: it is refused before any path is made from it
    """
    with pytest.raises(MajiError) as caught:
        library.version("count", "../../../../etc")
    assert caught.value.code == "invalid_argument"
