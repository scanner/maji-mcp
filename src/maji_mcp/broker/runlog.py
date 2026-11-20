"""
The run log: one row per run, one per stub call it made, and one per runtime library it
used.

It shows how skills are used, and which one-off scripts are worth promoting
(`docs/design/planned/agent-guidance.md`, "Getting better from the run log"). It
records which stubs were called and how they turned out, never the arguments or results,
which can hold work data.

For the runtime libraries, those a script can import beyond the standard library
(`maji_mcp.common.runtime_libraries`), it records which ones each run imported, how
often it called `help()` on each, the class of the exception a failed run ended with,
and whether that came from the library. It also records each time an agent read the
libraries' notes with `read_api`. These show whether agents need more than the notes;
`maji-library-use` prints them.
"""

# system imports
import argparse
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# project imports
from maji_mcp.common.runtime_libraries import LIBRARIES

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    runtime TEXT NOT NULL,
    script_sha256 TEXT,
    started_at TEXT NOT NULL,
    duration_ms INTEGER,
    exit_code INTEGER,
    timed_out INTEGER,
    output_bytes INTEGER,
    libraries_reported INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS calls (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    at TEXT NOT NULL,
    stub TEXT NOT NULL,
    outcome TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    result_bytes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_by_run ON calls(run_id);
CREATE TABLE IF NOT EXISTS runtime_libraries (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    library TEXT NOT NULL,
    imported INTEGER NOT NULL,
    asked INTEGER NOT NULL,
    error TEXT,
    raised_here INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS runtime_libraries_by_run ON runtime_libraries(run_id);
CREATE TABLE IF NOT EXISTS lookups (
    at TEXT NOT NULL,
    principal TEXT NOT NULL,
    name TEXT NOT NULL
);
"""

# A run log from before runs reported the libraries they used gets the column that says
# which did. A run that reported a listed library has rows in `runtime_libraries`, so
# it is marked; one that reported none left no trace, and stays unmarked.
#
MARK_REPORTED_RUNS = """
BEGIN;
ALTER TABLE runs ADD COLUMN libraries_reported INTEGER NOT NULL DEFAULT 0;
UPDATE runs SET libraries_reported = 1
WHERE run_id IN (SELECT run_id FROM runtime_libraries);
COMMIT;
"""

# A library's use over a period: the runs that imported it, those of them that failed,
# the failures raised inside it, the runs that called `help()` on it and how often, and
# how often agents read its notes.
#
LIBRARY_USE = """
SELECT l.library,
       SUM(l.imported),
       SUM(l.imported AND l.error IS NOT NULL),
       SUM(l.raised_here),
       SUM(l.asked > 0),
       SUM(l.asked)
FROM runtime_libraries l JOIN runs r ON r.run_id = l.run_id
WHERE r.started_at >= ?
GROUP BY l.library
"""

# The classes of the failures of runs that imported a library or failed inside it, by
# library: how many runs ended with each, and how many of those it raised.
#
LIBRARY_ERRORS = """
SELECT l.library, l.error, COUNT(*), SUM(l.raised_here)
FROM runtime_libraries l JOIN runs r ON r.run_id = l.run_id
WHERE r.started_at >= ? AND l.error IS NOT NULL AND (l.imported OR l.raised_here)
GROUP BY l.library, l.error
ORDER BY l.library, COUNT(*) DESC, l.error
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


###############################################################################
#
class RunLog:
    """
    The run log, in SQLite.

    Args:
        path: the database file, or `:memory:`
        read_only: open an existing log only to read it, as a report does beside the
            broker that writes it
    """

    def __init__(self, path: Path | str, read_only: bool = False) -> None:
        if read_only:
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            self.db = sqlite3.connect(uri, uri=True, isolation_level=None)
            return
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}
        if "libraries_reported" not in columns:
            self.db.executescript(MARK_REPORTED_RUNS)

    def start(
        self, run_id: str, principal: str, runtime: str, script_sha256: str | None
    ) -> None:
        self.db.execute(
            "INSERT INTO runs (run_id, principal, runtime, script_sha256, started_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (run_id, principal, runtime, script_sha256, _now()),
        )

    def finish(
        self,
        run_id: str,
        duration_ms: int,
        exit_code: int | None,
        timed_out: bool,
        output_bytes: int,
    ) -> None:
        self.db.execute(
            "UPDATE runs SET duration_ms = ?, exit_code = ?, timed_out = ?,"
            " output_bytes = ? WHERE run_id = ?",
            (duration_ms, exit_code, int(timed_out), output_bytes, run_id),
        )

    def call(
        self, run_id: str, stub: str, outcome: str, duration_ms: int, result_bytes: int
    ) -> None:
        """Record one stub call. `outcome` is `ok`, or the error's code."""
        self.db.execute(
            "INSERT INTO calls (run_id, at, stub, outcome, duration_ms, result_bytes)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, _now(), stub, outcome, duration_ms, result_bytes),
        )

    def run(self, run_id: str) -> dict[str, Any] | None:
        """A run's record: its principal, script hash, how it ended and how long it took."""
        row = self.db.execute(
            "SELECT principal, script_sha256, exit_code, timed_out, duration_ms"
            " FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        principal, script_sha256, exit_code, timed_out, duration_ms = row
        return {
            "principal": principal,
            "script_sha256": script_sha256,
            "exit_code": exit_code,
            "timed_out": bool(timed_out),
            "duration_ms": duration_ms,
        }

    def called(self, run_id: str) -> list[dict[str, Any]]:
        """
        What a run called, for its result: each stub or skill once, in the order it
        was first called, with how many calls there were and how many failed. The
        `maji` helpers are left out, being the run's own bookkeeping.
        """
        found: dict[str, dict[str, Any]] = {}
        for stub, outcome in self.calls(run_id):
            if stub.startswith("maji."):
                continue
            entry = found.setdefault(stub, {"name": stub, "calls": 0, "failed": 0})
            entry["calls"] += 1
            entry["failed"] += outcome != "ok"
        return list(found.values())

    def calls(self, run_id: str) -> list[tuple[str, str]]:
        """Return a run's calls as `(stub, outcome)` pairs, in order."""
        rows = self.db.execute(
            "SELECT stub, outcome FROM calls WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        )
        return [(stub, outcome) for stub, outcome in rows]

    def used(
        self,
        run_id: str,
        imported: set[str],
        asked: dict[str, int],
        error: str | None,
        raised_in: str | None,
    ) -> None:
        """
        Record the libraries a run used: a row for each library it imported, called
        `help()` on, or failed in. The run is marked as having reported, so it counts
        even when it used none.

        Args:
            run_id: the run
            imported: the libraries it imported
            asked: how many times it called `help()` on each library
            error: the class of the exception the run ended with, if it failed
            raised_in: the library that exception came from, if one did
        """
        self.db.execute(
            "UPDATE runs SET libraries_reported = 1 WHERE run_id = ?", (run_id,)
        )
        touched = imported | set(asked) | ({raised_in} if raised_in else set())
        self.db.executemany(
            "INSERT INTO runtime_libraries"
            " (run_id, library, imported, asked, error, raised_here)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    library,
                    int(library in imported),
                    asked.get(library, 0),
                    error,
                    int(library == raised_in),
                )
                for library in sorted(touched)
            ],
        )

    def looked_up(self, principal: str, name: str) -> None:
        """Record that an agent read the libraries' listing or a library's notes."""
        self.db.execute(
            "INSERT INTO lookups (at, principal, name) VALUES (?, ?, ?)",
            (_now(), principal, name),
        )

    def library_use(self, since: str) -> dict[str, Any]:
        """
        How runs used the libraries since a time.

        Only runs whose executor reported the libraries they used are counted. A run
        sends no report when it is stopped at its time or memory limit, or ran before
        executors reported.

        Args:
            since: the start of the period, as UTC ISO 8601

        Returns:
            `reported`, how many runs started in the period and reported;
            `unreported`, how many more started in it and sent no report;
            `importing`, how many reported runs imported a library; `listing_read`, how often agents read
            `read_api("python")`; and `libraries`, by name, each with `imported`,
            `failed` (runs that imported it and failed), `raised` (failures that came
            from it), `asked_runs` and `asked` (runs that called `help()` on it, and
            how many calls), and `notes_read`; and `errors`, by library, the classes
            of those failures, each with how many runs ended with it and how many of
            those the library raised, most frequent first
        """
        libraries: dict[str, dict[str, int]] = {}
        for name, *counts in self.db.execute(LIBRARY_USE, (since,)):
            libraries[name] = dict(
                zip(
                    ("imported", "failed", "raised", "asked_runs", "asked"),
                    counts,
                    strict=True,
                )
            )
        read = dict(
            self.db.execute(
                "SELECT name, COUNT(*) FROM lookups WHERE at >= ? GROUP BY name",
                (since,),
            ).fetchall()
        )
        for name, count in read.items():
            library = name.removeprefix("python.")
            if name.startswith("python.") and library in libraries:
                libraries[library]["notes_read"] = count
            elif name.startswith("python."):
                libraries[library] = {"notes_read": count}
        [(runs, reported)] = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(libraries_reported), 0) FROM runs"
            " WHERE started_at >= ?",
            (since,),
        )
        [(importing,)] = self.db.execute(
            "SELECT COUNT(DISTINCT l.run_id) FROM runtime_libraries l"
            " JOIN runs r ON r.run_id = l.run_id"
            " WHERE r.started_at >= ? AND r.libraries_reported AND l.imported",
            (since,),
        )
        errors: dict[str, list[tuple[str, int, int]]] = {}
        for name, error, count, raised in self.db.execute(LIBRARY_ERRORS, (since,)):
            errors.setdefault(name, []).append((error, count, raised))
        return {
            "reported": reported,
            "unreported": runs - reported,
            "importing": importing,
            "listing_read": read.get("python", 0),
            "libraries": libraries,
            "errors": errors,
        }


# The report's columns: each heading, and the count under it.
#
COLUMNS = (
    ("imported", "imported"),
    ("failed", "failed"),
    ("raised in it", "raised"),
    ("help() runs", "asked_runs"),
    ("help() calls", "asked"),
    ("notes read", "notes_read"),
)


def report(use: dict[str, Any], days: int, names: list[str]) -> str:
    """
    Lay out `RunLog.library_use` as a table: one row for each library, unused ones
    included.

    Args:
        use: what `library_use` returned
        days: how many days the period covers
        names: every library a script can import, in the order agents see them
    """
    lines = [
        f"In the last {_counted(days, 'day')}: {_counted(use['reported'], 'run')}"
        f" reported the libraries they used, of which {use['importing']} imported a"
        " listed library."
    ]
    if use["unreported"]:
        lines.append(
            f"Not counted: {_counted(use['unreported'], 'run')} that sent no report,"
            " from before reports began or stopped at a time or memory limit."
        )
    lines += [
        f"Agents read the libraries' listing {_counted(use['listing_read'], 'time')}.",
        "",
    ]
    width = max(len(n) for n in names) + 2
    lines.append("library".ljust(width) + "  ".join(h for h, _ in COLUMNS))
    for name in names:
        counts = use["libraries"].get(name, {})
        cells = [str(counts.get(key) or 0).rjust(len(h)) for h, key in COLUMNS]
        lines.append(name.ljust(width) + "  ".join(cells))
    failures = [name for name in names if use["errors"].get(name)]
    if failures:
        lines += [
            "",
            "What the failed runs ended with, and how many the library raised:",
        ]
    for name in failures:
        classes = ", ".join(
            f"{error} ×{count}" + (f" ({raised} raised in it)" if raised else "")
            for error, count, raised in use["errors"][name]
        )
        lines.append(f"  {name}: {classes}")
    return "\n".join(lines)


def _counted(count: int, noun: str) -> str:
    """`count` and `noun`, plural unless the count is 1: `1 run`, `3 runs`."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def main(argv: list[str] | None = None) -> None:
    """Print how scripts used each library a script can import, from the run log."""
    parser = argparse.ArgumentParser(
        prog="maji-library-use",
        description="Print how scripts used each library they can import, from the "
        "run log the broker keeps.",
    )
    parser.add_argument(
        "--run-log", type=Path, required=True, help="the broker's run log, runs.db"
    )
    parser.add_argument(
        "--days", type=int, default=30, help="how many days back to count (30)"
    )
    args = parser.parse_args(argv)
    since = datetime.now(UTC) - timedelta(days=args.days)
    use = RunLog(args.run_log, read_only=True).library_use(
        since.isoformat(timespec="milliseconds")
    )
    print(report(use, args.days, [library.name for library in LIBRARIES]))
