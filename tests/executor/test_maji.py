"""
Tests for the `maji` module's helpers over mounted files: `git`, `git_log`, `blame`
and `sqlite` (`src/maji_mcp/executor/maji.py`).

They run the client on this machine, against a repository and log captures built in a
temporary folder that stands in for `/mnt`, with this machine's git. The Docker test in
`tests/docker/test_mounts.py` runs them in a real executor, on a read-only mount.
"""

# system imports
import importlib.util
import sqlite3
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

# 3rd party imports
import pytest
from pytest_check import check

# project imports
from tests.support.paths import MAJI_MODULE
from tests.support.repositories import (
    build_capture,
    build_repository,
    git,
    publish_snapshot,
)


@pytest.fixture(scope="module")
def mnt(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """
    A folder that stands in for `/mnt`, holding a repository and a folder of log
    captures.

    Building it runs git and two writers, so it is built once for the module: the
    tests read it and never change it. A test that changes a repository builds its
    own.
    """
    mnt = tmp_path_factory.mktemp("mnt")
    build_repository(mnt / "api")
    build_capture(mnt / "captures")
    return mnt


@pytest.fixture
def maji(mnt: Path) -> ModuleType:
    """The `maji` module as a script has it, with `mnt` as its `/mnt`."""
    spec = importlib.util.spec_from_file_location("maji", MAJI_MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "MOUNTS", mnt)  # noqa: B010 - a module global
    return module


def test_git_log_returns_commits_with_the_files_each_changed(maji: ModuleType) -> None:
    """
    GIVEN: A mounted repository with three commits
     WHEN: the script calls maji.git_log
     THEN: it gets the commits newest first, each with its author, date and subject,
           and the lines added and removed in each file it changed
      AND: a binary file's counts are None, and a path with a space comes through whole
    """
    commits = maji.git_log("api")
    assert [c["subject"] for c in commits] == [
        "ABC-3 shout three",
        "ABC-2 shout two",
        "ABC-1 start the app",
    ]

    # Each field of a commit is its own fact about what a script is given.
    #
    with check:
        assert commits[1]["author"] == "Bo"
    with check:
        assert commits[1]["email"] == "bo@example.com"
    with check:
        assert commits[1]["date"] == "2026-01-05T12:00:00+00:00"
    with check:
        assert commits[1]["files"] == [
            {"path": "app.py", "added": 2, "removed": 1},
            {"path": "docs dir/notes.md", "added": 1, "removed": 0},
        ]
    with check:
        assert {"path": "logo.bin", "added": None, "removed": None} in commits[2][
            "files"
        ]
    with check:
        assert all(len(c["sha"]) == 40 for c in commits)


@pytest.mark.parametrize(
    ("narrowing", "subjects"),
    [
        ({"since": "2026-02-01"}, ["ABC-3 shout three"]),
        ({"until": "2026-01-03"}, ["ABC-1 start the app"]),
        ({"paths": ["docs dir"]}, ["ABC-2 shout two"]),
        ({"max_count": 2}, ["ABC-3 shout three", "ABC-2 shout two"]),
    ],
    ids=["since a day", "until a day", "under a path", "at most two"],
)
def test_git_log_narrows_by_time_path_and_count(
    maji: ModuleType, narrowing: dict[str, Any], subjects: list[str]
) -> None:
    """
    GIVEN: A mounted repository with three commits over six weeks
     WHEN: the script asks maji.git_log for a time range, a path, or a count
     THEN: only the commits that fit come back, newest first
    """
    assert [c["subject"] for c in maji.git_log("api", **narrowing)] == subjects


def test_blame_names_the_commit_behind_each_line(maji: ModuleType) -> None:
    """
    GIVEN: A mounted repository where lines of app.py were changed in different
           commits
     WHEN: the script calls maji.blame for one line, and for the whole file
     THEN: each line comes with its number, its text, and the commit, author, date
           and subject that last changed it
    """
    [line] = maji.blame("api", "app.py", 2)
    with check:
        assert (line["line"], line["text"]) == (2, "TWO")
    with check:
        assert line["subject"] == "ABC-2 shout two"
    with check:
        assert (line["author"], line["email"]) == ("Bo", "bo@example.com")
    with check:
        assert line["date"] == "2026-01-05T12:00:00+00:00"
    whole = maji.blame("api", "app.py")
    assert [(b["line"], b["subject"]) for b in whole] == [
        (1, "ABC-1 start the app"),
        (2, "ABC-2 shout two"),
        (3, "ABC-3 shout three"),
        (4, "ABC-2 shout two"),
    ]


def test_a_failure_says_what_git_said(maji: ModuleType) -> None:
    """
    GIVEN: A script naming a mount that doesn't exist, or a revision that doesn't
     WHEN: it calls the helpers
     THEN: it gets a StubError: `not_found` listing what is mounted, or
           `invalid_argument` with git's own message
    """
    with pytest.raises(maji.StubError) as missing:
        maji.git_log("web")
    assert missing.value.code == "not_found"
    assert missing.value.data == {"mounts": ["api", "captures"]}
    with pytest.raises(maji.StubError) as bad:
        maji.git_log("api", rev="no-such-branch")
    assert bad.value.code == "invalid_argument"
    assert "no-such-branch" in bad.value.message


def test_a_repositorys_config_cant_run_commands_through_the_helpers(
    maji: ModuleType, tmp_path: Path
) -> None:
    """
    GIVEN: A mounted repository whose own config names commands for git to run: a
           filesystem monitor, and a textconv filter for the files it changed
     WHEN: the script calls maji.git("status"), maji.git_log and maji.blame, and
           passes a revision that reads as an option
     THEN: none of those commands runs, and the revision is taken as a revision
    """
    # This test changes the repository's config, so it reads a repository of its own.
    #
    repo = tmp_path / "mnt" / "api"
    build_repository(repo)
    setattr(maji, "MOUNTS", tmp_path / "mnt")  # noqa: B010 - a module global
    marker = tmp_path / "ran"
    hook = tmp_path / "hook.sh"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat "$1" 2>/dev/null\n')
    hook.chmod(0o755)
    (repo / ".gitattributes").write_text("*.py diff=evil\n")
    git(repo, "config", "core.fsmonitor", str(hook))
    git(repo, "config", "diff.evil.textconv", str(hook))

    # Plain git runs both commands, so the checks below show the helpers turn them
    # off, not that git never would have run them.
    #
    for plain in (["git", "status"], ["git", "log", "-p", "-1"]):
        subprocess.run(plain, cwd=repo, capture_output=True, check=True)
        assert marker.exists(), plain
        marker.unlink()

    maji.git("api", "status")
    maji.git_log("api")
    maji.blame("api", "app.py")
    with pytest.raises(maji.StubError):
        maji.git_log("api", rev=f"--output={marker}")
    with pytest.raises(maji.StubError):
        maji.blame("api", "app.py", rev=f"--output={marker}")
    assert not marker.exists()


@pytest.mark.parametrize(
    ("capture", "left_in_its_wal"),
    [("closed.db", False), ("killed.db", True)],
    ids=["closed cleanly", "its writer killed"],
)
def test_sqlite_reads_every_row_of_a_capture_however_it_stopped(
    maji: ModuleType, mnt: Path, capture: str, left_in_its_wal: bool
) -> None:
    """
    GIVEN: A mounted folder of captures: one closed cleanly, and one whose writer was
           killed with most of its rows still in its `-wal` file
     WHEN: the script opens one with maji.sqlite and counts the rows
     THEN: it counts all 100, for the killed one too, rather than only the rows that
           had reached the database file
    """
    wal = mnt / "captures" / f"{capture}-wal"
    assert wal.exists() is left_in_its_wal
    db = maji.sqlite("captures", capture)
    assert db.execute("SELECT COUNT(*) FROM logs").fetchone() == (100,)


def test_sqlite_opens_read_only(maji: ModuleType) -> None:
    """
    GIVEN: A mounted capture
     WHEN: the script opens it with maji.sqlite and tries to change it, or names a
           database that isn't there
     THEN: the change is refused by SQLite, and the missing one is `not_found`
    """
    db = maji.sqlite("captures", "closed.db")
    with pytest.raises(sqlite3.OperationalError):
        db.execute("DELETE FROM logs")
    with pytest.raises(maji.StubError) as missing:
        maji.sqlite("captures", "nope.db")
    assert missing.value.code == "not_found"


def test_sqlite_reads_the_newest_snapshot_through_its_link(
    maji: ModuleType, tmp_path: Path
) -> None:
    """
    GIVEN: A mounted folder of snapshots of a capture still being written, each a
           complete database under a name of its own, and `worker.db`, a link to the
           newest
     WHEN: the script opens `worker.db` with maji.sqlite, a newer snapshot replaces it
           and the older one is deleted, and the script opens `worker.db` again
     THEN: the first connection goes on reading the snapshot it opened
      AND: the second reads the newer one
    """
    setattr(maji, "MOUNTS", tmp_path)  # noqa: B010 - a module global
    count = "SELECT COUNT(*) FROM logs"
    first = publish_snapshot(tmp_path / "snapshots", "1", 100)
    held = maji.sqlite("snapshots", "worker.db")
    before = held.execute(count).fetchone()
    publish_snapshot(tmp_path / "snapshots", "2", 150)
    first.unlink()
    with check:
        assert (before, held.execute(count).fetchone()) == ((100,), (100,))
    with check:
        assert maji.sqlite("snapshots", "worker.db").execute(count).fetchone() == (150,)
