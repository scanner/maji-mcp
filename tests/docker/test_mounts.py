"""
Tests that granted paths reach a real executor read-only, and nothing else does.

They need a runtime image with git, and Docker. The fixtures live under your home
directory, the only place Rancher Desktop shares with its VM, in a folder of their own
that is removed afterwards. `MAJI_RUNTIME_IMAGE` names the image (`tests/docker`), so
a build is tried before it replaces the stack's: `make test-docker` does that.
"""

# system imports
import json
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

# 3rd party imports
import pytest
from pytest_check import check

# project imports
from maji_mcp.runner.docker_runner import CoverLost, DockerRunner, RunResult
from maji_mcp.runner.mounts import MountRules, covers_in, named, resolve_all
from maji_mcp.runner.sandbox import Cover, Mount
from tests.support.repositories import (
    build_capture,
    build_repository,
    publish_snapshot,
)

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def place() -> Iterator[Path]:
    """
    A folder under your home directory holding a repository, with a symlink in it to
    a file beside it, outside the repository, a folder of log captures, and a folder
    of two snapshots of a capture with a link to the newer.

    Building it runs git and two writers, and a read-only mount can't change it, so
    the module shares one. A test that changes a repository builds its own inside.
    """
    base = Path.home() / ".cache" / "maji-mcp" / "test-mounts"
    base.mkdir(parents=True, exist_ok=True)
    place = Path(tempfile.mkdtemp(dir=base))
    try:
        build_repository(place / "api")
        (place / "outside.txt").write_text("not granted")
        (place / "api" / "escape").symlink_to(place / "outside.txt")
        build_capture(place / "captures")
        publish_snapshot(place / "snapshots", "1", 100)
        publish_snapshot(place / "snapshots", "2", 150)
        yield place
    finally:
        shutil.rmtree(place)


@pytest.fixture
def granted(place: Path) -> Callable[..., list[Mount]]:
    """
    A function that turns paths under `place` into mounts, as the runner does for a
    run whose paths were all granted.
    """
    rules = MountRules.of([place])

    def mounts(*paths: Path) -> list[Mount]:
        return named(resolve_all([str(path) for path in paths], rules))

    return mounts


def test_a_granted_repository_is_read_with_git_and_never_written(
    run_script: Callable[..., RunResult],
    granted: Callable[..., list[Mount]],
    place: Path,
) -> None:
    """
    GIVEN: A repository granted to a run, resolved as the runner resolves it, holding
           a symlink to a file beside it, outside the grant
     WHEN: the run's script lists its mounts, reads the history with maji.git_log
           and maji.blame, tries to write in the repository, and follows the symlink
     THEN: the history and blame come back from inside the executor
      AND: every write fails, in the files and in .git alike
      AND: the symlink leads nowhere: it resolves in the executor's own filesystem,
           where only the granted folder is mounted
    """
    code = """
import json
results = {"mounts": sorted(maji.mounts()), "writes": {}}
results["subjects"] = [c["subject"] for c in maji.git_log("api")]
results["blamed"] = maji.blame("api", "app.py", 2)[0]["subject"]
for path in ["/mnt/api/app.py", "/mnt/api/.git/config", "/mnt/api/new.txt"]:
    try:
        with open(path, "a") as fh:
            fh.write("x")
        results["writes"][path] = "written"
    except OSError as exc:
        results["writes"][path] = type(exc).__name__
try:
    results["escape"] = open("/mnt/api/escape").read()
except OSError as exc:
    results["escape"] = type(exc).__name__
print(json.dumps(results))
"""
    result = run_script(code, mounts=granted(place / "api"))
    assert result.exit_code == 0, result.stderr
    results = json.loads(result.stdout)

    # What the script read, what it couldn't write, and where the symlink led, each
    # checked on its own.
    #
    with check:
        assert results["mounts"] == ["api"]
    with check:
        assert results["subjects"] == [
            "ABC-3 shout three",
            "ABC-2 shout two",
            "ABC-1 start the app",
        ]
    with check:
        assert results["blamed"] == "ABC-2 shout two"
    with check:
        assert len(results["writes"]) == 3
        assert "written" not in results["writes"].values()
    with check:
        assert results["escape"] == "FileNotFoundError"


def test_gitpython_reads_a_mount_and_no_git_runs_its_repositorys_hook(
    run_script: Callable[..., RunResult],
    granted: Callable[..., list[Mount]],
    place: Path,
) -> None:
    """
    GIVEN: A granted repository whose own config names a command, inside it, as its
           filesystem monitor, which git runs on `git status`
     WHEN: the run's script runs `git status` with plain git and through GitPython,
           and reads the history with GitPython
     THEN: neither runs the command, since the image's environment turns the monitor
           off for every git process
      AND: GitPython reads the history like any repository
    """
    # This test changes the repository's config, so it builds one of its own.
    #
    repo = place / "hooked"
    build_repository(repo)
    hook = repo / ".git" / "monitor.sh"
    hook.write_text("#!/bin/sh\ntouch /tmp/ran\n")
    hook.chmod(0o755)
    config = repo / ".git" / "config"
    config.write_text(
        config.read_text() + "[core]\n\tfsmonitor = /mnt/hooked/.git/monitor.sh\n"
    )

    # The first `git status` runs with the image's GIT_CONFIG_* settings cleared, to
    # show the monitor does run when nothing turns it off.
    #
    code = """
import json, os, subprocess, git

def ran(do):
    if os.path.exists("/tmp/ran"):
        os.remove("/tmp/ran")
    do()
    return os.path.exists("/tmp/ran")

bare = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")}
status = ["git", "-C", "/mnt/hooked", "status"]
print(json.dumps({
    "settings cleared": ran(lambda: subprocess.run(status, env=bare, capture_output=True)),
    "plain git": ran(lambda: subprocess.run(status, capture_output=True)),
    "GitPython": ran(lambda: git.Repo("/mnt/hooked").git.status()),
    "subjects": [c.summary for c in git.Repo("/mnt/hooked").iter_commits()],
}))
"""
    result = run_script(code, mounts=granted(repo))
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "settings cleared": True,
        "plain git": False,
        "GitPython": False,
        "subjects": ["ABC-3 shout three", "ABC-2 shout two", "ABC-1 start the app"],
    }


def test_a_capture_is_read_whole_through_a_read_only_mount(
    run_script: Callable[..., RunResult],
    granted: Callable[..., list[Mount]],
    place: Path,
) -> None:
    """
    GIVEN: A granted folder of log captures in WAL mode: one closed cleanly, and one
           whose writer was killed with most of its rows still in its `-wal` file
     WHEN: the run's script opens each with maji.sqlite and counts the rows
     THEN: both count all 100 rows. Of SQLite's two read-only ways in, each fails on
           one of them through a read-only mount: `mode=ro` can't open the closed one,
           and `immutable` misses the killed one's newest rows.
    """
    code = """
import json, sqlite3
count = "SELECT COUNT(*) FROM logs"
results = {
    name: maji.sqlite("captures", name).execute(count).fetchone()[0]
    for name in ("closed.db", "killed.db")
}
try:
    sqlite3.connect("file:/mnt/captures/closed.db?mode=ro", uri=True).execute(count)
    results["closed, mode=ro"] = "opened"
except sqlite3.OperationalError as exc:
    results["closed, mode=ro"] = str(exc)
immutable = "file:/mnt/captures/killed.db?immutable=1"
results["killed, immutable"] = sqlite3.connect(immutable, uri=True).execute(count).fetchone()[0]
print(json.dumps(results))
"""
    result = run_script(code, mounts=granted(place / "captures"))
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "closed.db": 100,
        "killed.db": 100,
        "closed, mode=ro": "unable to open database file",
        "killed, immutable": 1,
    }


def test_a_snapshot_is_read_through_the_link_inside_its_folder(
    run_script: Callable[..., RunResult],
    granted: Callable[..., list[Mount]],
    place: Path,
) -> None:
    """
    GIVEN: A granted folder of snapshots of a capture, as a capture tool publishes
           them while it writes, with `worker.db`, a link relative to the folder, to
           the newest
     WHEN: the run's script lists the folder's links and opens `worker.db` with
           maji.sqlite
     THEN: the link leads to the newest snapshot inside the mount, and every one of
           its rows is read
    """
    code = """
import json, os
links = sorted(n for n in os.listdir("/mnt/snapshots") if os.path.islink(f"/mnt/snapshots/{n}"))
rows = maji.sqlite("snapshots", "worker.db").execute("SELECT COUNT(*) FROM logs").fetchone()[0]
print(json.dumps({"links": links, "newest": os.readlink("/mnt/snapshots/worker.db"), "rows": rows}))
"""
    result = run_script(code, mounts=granted(place / "snapshots"))
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "links": ["worker.db"],
        "newest": "worker.2.db",
        "rows": 150,
    }


def test_a_path_not_granted_is_refused_before_any_container_exists(
    docker_runner: DockerRunner, place: Path
) -> None:
    """
    GIVEN: A run whose mounts include a path the broker didn't grant
     WHEN: the runner is asked to run it
     THEN: it refuses before creating a container, and no container is left
    """
    label = {"label": "maji-mcp/run_id=test-ungranted"}
    with pytest.raises(ValueError, match="only of a granted path"):
        docker_runner.run(
            "python",
            "print('ran')",
            "test-ungranted",
            "test-token",
            mounts=[Mount(str(place), "everything")],
            granted={str(place / "api")},
        )
    assert docker_runner.client.containers.list(all=True, filters=label) == []


def test_what_a_mount_leaves_out_reads_as_empty_and_stays_that_way(
    run_script: Callable[..., RunResult], place: Path
) -> None:
    """
    GIVEN: A granted repository holding a `.env` file, a link to it, a folder on your
           never-mount list with a file in it, and a remote URL with a token in its
           git config
     WHEN: the run's script, mounted with what covers_in leaves out, reads each of
           them, lists the folder, reads the code and the history, and tries to
           write over the `.env` and to take its cover off
     THEN: the `.env`, the link to it and the git config each read as empty, and the
           folder lists as empty, while the script still sees that each is there
      AND: the code beside them reads whole, and git still reads the history
      AND: your `.env` holds what it held, and the cover can't be taken off
    """
    # This test adds files to the repository, so it builds one of its own.
    #
    own = place / "left-out"
    api = own / "api"
    build_repository(api)
    (api / ".env").write_text("TOKEN=only-on-the-host\n")
    (api / "link-to-env").symlink_to(".env")
    (api / "private").mkdir()
    (api / "private" / "notes.txt").write_text("PRIVATE\n")
    with open(api / ".git" / "config", "a") as config:
        config.write('[remote "origin"]\n\turl = https://me:ghp_abc@example.com/api\n')
    listed = own / "never-mount"
    listed.write_text(f"{api / 'private'}\n")

    # What the runner leaves out of the mount: the two files that hold credentials,
    # and the folder on the list.
    #
    rules = MountRules.of([place], never_file=listed).current()
    mounts = named(resolve_all([str(api)], rules))
    out = covers_in(Path(mounts[0].source), rules, time.monotonic() + 30)
    assert sorted(out.secrets) == [".env", ".git/config"]
    assert out.listed == (("private", True),)

    # The script reads everything, then tries two ways to get at the `.env`: writing
    # over it, and unmounting its cover.
    #
    code = """
import json, os, subprocess
base = "/mnt/api"
results = {
    "listing": sorted(os.listdir(base)),
    "env": open(base + "/.env").read(),
    "link": open(base + "/link-to-env").read(),
    "config": open(base + "/.git/config").read(),
    "private": os.listdir(base + "/private"),
    "code": open(base + "/app.py").read(),
    "subjects": [c["subject"] for c in maji.git_log("api")],
}
try:
    open(base + "/private/new.txt", "w").write("x")
    results["wrote_private"] = True
except OSError as exc:
    results["wrote_private"] = type(exc).__name__
with open(base + "/.env", "w") as fh:
    fh.write("OVERWRITTEN")
results["env_after_write"] = open(base + "/.env").read()
results["umount"] = subprocess.run(["umount", base + "/.env"], capture_output=True).returncode
results["env_after_umount"] = open(base + "/.env").read()
print(json.dumps(results))
"""
    result = run_script(code, mounts=mounts, covers=out.covers(mounts[0].name))
    assert result.exit_code == 0, result.stderr
    results = json.loads(result.stdout)

    # Each thing the script saw is checked on its own, so one run shows every cover
    # that didn't hold.
    #
    with check:
        assert {".env", "link-to-env", "private", "app.py"} <= set(results["listing"])
    with check:
        assert results["env"] == results["link"] == results["config"] == ""
    with check:
        assert results["private"] == []
    with check:
        assert results["code"] == "one\nTWO\nTHREE\nfour\n"
    with check:
        assert results["subjects"][0] == "ABC-3 shout three"
    with check:
        assert results["wrote_private"] == "OSError"
    with check:
        assert results["env_after_write"] == ""
    with check:
        assert results["umount"] != 0 and results["env_after_umount"] == ""
    with check:
        assert (api / ".env").read_text() == "TOKEN=only-on-the-host\n"


def test_a_cover_whose_file_has_gone_runs_nothing(
    run_script: Callable[..., RunResult],
    granted: Callable[..., list[Mount]],
    docker_runner: DockerRunner,
    place: Path,
) -> None:
    """
    GIVEN: A granted repository, and a cover for a file removed since it was found
     WHEN: the run is started
     THEN: nothing runs: the runner says a cover was lost, and leaves no container
           behind
    """
    containers = docker_runner.client.containers
    before = {c.id for c in containers.list(all=True)}
    with pytest.raises(CoverLost):
        run_script(
            "print('ran')",
            mounts=granted(place / "api"),
            covers=[Cover("api", "gone.env")],
        )
    assert {c.id for c in containers.list(all=True)} <= before
