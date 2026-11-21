"""
Builders for what a script reads through a mount: a git repository with a known
history, SQLite log captures stopped in two ways, and snapshots of a capture.
"""

# system imports
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def git(repo: Path, *args: str, author: str = "Ann", when: str = "2026-01-02") -> None:
    """Run git in `repo` as `author`, at noon UTC on `when`, as the fixture's history."""
    stamp = f"{when}T12:00:00+00:00"
    email = f"{author.lower()}@example.com"

    # Git's GIT_AUTHOR_* and GIT_COMMITTER_* variables outrank its config, and a CI
    # runner may set them to the commit being tested, so the fixture sets all of them.
    #
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": author,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_NAME": author,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_COMMITTER_DATE": stamp,
        },
    )


def build_repository(repo: Path) -> None:
    """
    Three commits: a Python file and a binary file; a change to the Python file and a
    new file in a folder with a space; then, a month later, one line changed again.
    """
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("one\ntwo\nthree\n")
    (repo / "logo.bin").write_bytes(b"\x00\x01binary")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "ABC-1 start the app")
    (repo / "app.py").write_text("one\nTWO\nthree\nfour\n")
    (repo / "docs dir").mkdir()
    (repo / "docs dir" / "notes.md").write_text("hello\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "ABC-2 shout two", author="Bo", when="2026-01-05")
    (repo / "app.py").write_text("one\nTWO\nTHREE\nfour\n")
    git(repo, "commit", "-qam", "ABC-3 shout three", when="2026-02-10")


# A writer that logs `rows` lines into a database in WAL mode, as a capture tool does.
# With checkpoints off, every row stays in the `-wal` file until a clean close moves
# them into the database; `os._exit` stops it the way a killed capture stops, leaving
# the `-wal` file behind.
#
WRITER = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA wal_autocheckpoint=0")
db.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, severity TEXT)")
db.execute("INSERT INTO logs (severity) VALUES ('INFO')")
db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
for _ in range(int(sys.argv[2]) - 1):
    db.execute("INSERT INTO logs (severity) VALUES ('ERROR')")
if sys.argv[3] == "killed":
    os._exit(0)
db.close()
"""


def build_capture(folder: Path) -> None:
    """
    Two log captures of 100 rows: `closed.db`, closed cleanly, and `killed.db`, whose
    writer stopped without closing, with 99 of its rows still in `killed.db-wal`.
    """
    folder.mkdir(parents=True)
    for name, how in (("closed.db", "closed"), ("killed.db", "killed")):
        subprocess.run(
            [sys.executable, "-c", WRITER, str(folder / name), "100", how], check=True
        )


def publish_snapshot(folder: Path, stamp: str, rows: int) -> Path:
    """
    Publish a snapshot of a capture of `rows` rows into `folder`, as a capture tool does
    while it writes: a complete database under a name of its own, `worker.<stamp>.db`,
    then `worker.db`, a link relative to the folder, moved to it in one step.

    Returns:
        The snapshot
    """
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = folder / f"worker.{stamp}.db"
    db = sqlite3.connect(snapshot)
    db.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, severity TEXT)")
    db.executemany("INSERT INTO logs (severity) VALUES (?)", [("INFO",)] * rows)
    db.commit()
    db.close()
    staged = folder / ".worker.db.next"
    staged.symlink_to(snapshot.name)
    staged.replace(folder / "worker.db")
    return snapshot
