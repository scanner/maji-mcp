"""
Tests for the run log's account of the libraries scripts used, and the report
`maji-library-use` prints from it (`src/maji_mcp/broker/runlog.py`).
"""

# system imports
import sqlite3
from pathlib import Path

# 3rd party imports
import pytest
from pytest_check import check

# project imports
from maji_mcp.broker.runlog import RunLog, main, report
from maji_mcp.common.runtime_libraries import LIBRARIES

# The start of the period the tests count from; runs started before it are left out.
#
SINCE = "2026-11-10T00:00:00.000+00:00"


@pytest.fixture
def runlog(tmp_path: Path) -> RunLog:
    """
    A run log on disk holding seven runs:

    - `early`, before SINCE, which imported pandas
    - `sift`, which imported pandas and duckdb, and asked help() about pandas 3 times
    - `fail`, which imported pandas and failed with a KeyError raised in pandas
    - `typo`, which imported pandas and failed with a NameError of its own
    - `yaml`, which imported yaml
    - `plain`, which imported none of the listed libraries
    - `silent`, which sent no report, as a run stopped at its time limit doesn't

    and three reads of the notes: the listing once, and pandas's twice.
    """
    log = RunLog(tmp_path / "runs.db")
    runs = {
        "early": ("2026-11-01T09:00:00.000+00:00", {"pandas"}, {}, None, None),
        "sift": ("2026-11-12T09:00:00.000+00:00", {"pandas", "duckdb"}, {"pandas": 3}, None, None),
        "fail": ("2026-11-12T10:00:00.000+00:00", {"pandas"}, {}, "KeyError", "pandas"),
        "typo": ("2026-11-12T11:00:00.000+00:00", {"pandas"}, {}, "NameError", None),
        "yaml": ("2026-11-13T09:00:00.000+00:00", {"yaml"}, {}, None, None),
        "plain": ("2026-11-13T10:00:00.000+00:00", set(), {}, None, None),
    }  # fmt: skip
    for run_id, (started, imported, asked, error, raised_in) in runs.items():
        log.start(run_id, "reader", "python", None)
        log.db.execute(
            "UPDATE runs SET started_at = ? WHERE run_id = ?", (started, run_id)
        )
        log.used(run_id, imported, asked, error, raised_in)
    log.start("silent", "reader", "python", None)
    for name in ("python", "python.pandas", "python.pandas"):
        log.looked_up("reader", name)
    return log


def test_library_use_counts_each_librarys_runs_failures_and_questions(
    runlog: RunLog,
) -> None:
    """
    GIVEN: Runs before and after the start of a period, which imported, asked about
           and failed in libraries, a run that sent no report, and reads of the
           libraries' notes
     WHEN: the run log counts the libraries' use since the start of the period
     THEN: only runs in the period that reported count: five runs, four of which
           imported a listed library
      AND: the run in the period that sent no report is counted apart
      AND: for pandas, three runs imported it, two of them failed, one failure was
           raised inside it, and one run asked help() three times about it
      AND: the reads of the notes are counted by library, and the listing's apart
    """
    use = runlog.library_use(SINCE)
    with check:
        assert (use["reported"], use["unreported"]) == (5, 1)
    with check:
        assert (use["importing"], use["listing_read"]) == (4, 1)
    with check:
        assert use["libraries"]["pandas"] == {
            "imported": 3,
            "failed": 2,
            "raised": 1,
            "asked_runs": 1,
            "asked": 3,
            "notes_read": 2,
        }
    with check:
        assert use["libraries"]["duckdb"]["imported"] == 1
    with check:
        assert set(use["libraries"]) == {"pandas", "duckdb", "yaml"}


def test_the_report_has_a_row_for_every_library_used_or_not(runlog: RunLog) -> None:
    """
    GIVEN: The libraries' use over a period, in which some libraries were never used,
           and runs that imported pandas failed with two classes of exception
     WHEN: the report is laid out
     THEN: it says how many runs reported the libraries they used and how many of
           them imported one, and how many runs sent no report and aren't counted
      AND: it gives a row for every library a script can import, in the order agents
           see them, with zeros for those nobody used
      AND: after the table, it names the classes the failed runs ended with, for each
           library with failures, and how many of each the library raised
    """
    names = [library.name for library in LIBRARIES]
    lines = report(runlog.library_use(SINCE), 7, names).splitlines()
    heading = next(i for i, line in enumerate(lines) if line.startswith("library "))
    table = lines[heading + 1 : heading + 1 + len(names)]
    with check:
        assert lines[:3] == [
            "In the last 7 days: 5 runs reported the libraries they used, of which 4"
            " imported a listed library.",
            "Not counted: 1 run that sent no report, from before reports began or"
            " stopped at a time or memory limit.",
            "Agents read the libraries' listing 1 time.",
        ]
    rows = {line.split()[0]: line.split()[1:] for line in table}
    with check:
        assert list(rows) == names
    with check:
        assert rows["pandas"] == ["3", "2", "1", "1", "3", "2"]
    with check:
        assert rows["jinja2"] == ["0"] * 6
    with check:
        assert lines[heading + 1 + len(names) + 2 :] == [
            "  pandas: KeyError ×1 (1 raised in it), NameError ×1"
        ]


def test_a_read_only_log_reads_the_brokers_rows_and_cannot_change_them(
    runlog: RunLog, tmp_path: Path
) -> None:
    """
    GIVEN: The broker's run log, which the broker keeps open and writes
     WHEN: a report opens the same file read-only
     THEN: it sees the broker's rows, and any write through it is refused
    """
    reader = RunLog(tmp_path / "runs.db", read_only=True)
    with check:
        assert reader.library_use(SINCE)["reported"] == 5
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.looked_up("reader", "python")


def test_maji_library_use_prints_the_report(
    runlog: RunLog, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    GIVEN: A run log with runs in the last days
     WHEN: `maji-library-use --run-log runs.db --days 3650` runs, a period that holds
           every run
     THEN: it prints the report for that period, counting all six runs that reported
    """
    main(["--run-log", str(tmp_path / "runs.db"), "--days", "3650"])
    printed = capsys.readouterr().out
    assert printed.startswith("In the last 3650 days: 6 runs reported the libraries")


def test_a_log_from_before_reports_learns_which_runs_reported(tmp_path: Path) -> None:
    """
    GIVEN: A run log written before runs were marked as reporting, holding a run with
           a row for a library it imported, and a run with none
     WHEN: the broker opens it
     THEN: the run with a row counts as having reported, and the other doesn't
    """
    old = sqlite3.connect(tmp_path / "runs.db", isolation_level=None)
    old.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, principal TEXT NOT NULL, runtime TEXT NOT NULL,
            script_sha256 TEXT, started_at TEXT NOT NULL, duration_ms INTEGER,
            exit_code INTEGER, timed_out INTEGER, output_bytes INTEGER
        );
        CREATE TABLE runtime_libraries (
            run_id TEXT NOT NULL REFERENCES runs(run_id), library TEXT NOT NULL,
            imported INTEGER NOT NULL, asked INTEGER NOT NULL, error TEXT,
            raised_here INTEGER NOT NULL
        );
        INSERT INTO runs (run_id, principal, runtime, started_at) VALUES
            ('yaml', 'reader', 'python', '2026-11-12T09:00:00.000+00:00'),
            ('older', 'reader', 'python', '2026-11-12T10:00:00.000+00:00');
        INSERT INTO runtime_libraries VALUES ('yaml', 'yaml', 1, 0, NULL, 0);
        """
    )
    old.close()
    use = RunLog(tmp_path / "runs.db").library_use(SINCE)
    assert (use["reported"], use["unreported"], use["importing"]) == (1, 1, 1)
