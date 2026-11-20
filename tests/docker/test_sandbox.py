"""
Tests that the executor sandbox holds, against a real Docker daemon
(`src/maji_mcp/runner/sandbox.py`, `docker_runner.py`).

Each test runs a short script in a fresh executor container and checks what it could
do. They need the runtime image, which `MAJI_RUNTIME_IMAGE` names (`tests/docker`):
`make test-docker` builds one for them.
"""

# system imports
import contextlib
import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

# 3rd party imports
import docker
import pytest
from pytest_check import check

# project imports
from maji_mcp.runner.docker_runner import DockerRunner, RunnerStopping, RunResult
from maji_mcp.runner.sandbox import DEFAULT_LIMITS, SandboxSettings, container_config

pytestmark = pytest.mark.docker

# What a script can find out about the box it runs in, gathered in one run: each
# probe tries something the sandbox is there to stop, and reports what happened.
#
PROBE = """
import json, os, socket

found = {"network": {}, "writes": {}}
for host, port in [("1.1.1.1", 53), ("host.docker.internal", 80), ("example.com", 443)]:
    try:
        socket.create_connection((host, port), timeout=3).close()
        found["network"][host] = "connected"
    except OSError as exc:
        found["network"][host] = type(exc).__name__

for path in ["/work/a", "/tmp/a", "/a", "/etc/a", "/opt/maji/maji.py", "/usr/local/a"]:
    try:
        with open(path, "w") as fh:
            fh.write("x")
        found["writes"][path] = "written"
    except OSError as exc:
        found["writes"][path] = type(exc).__name__

found["environment"] = sorted(os.environ)
sockets = ["/var/run/docker.sock", "/run/docker.sock", "/run/maji/docker.sock"]
found["docker_sockets"] = [p for p in sockets if os.path.exists(p)]

status = dict(
    line.split(":\\t", 1) for line in open("/proc/self/status").read().splitlines()
    if ":\\t" in line
)
found["process"] = {
    "uid": os.getuid(), "gid": os.getgid(),
    "cap_eff": status["CapEff"].strip(), "no_new_privs": status["NoNewPrivs"].strip(),
}
print(json.dumps(found))
"""


def test_a_script_is_boxed_in(run_script: Callable[..., RunResult]) -> None:
    """
    GIVEN: A script that tries to connect to the internet and to the host, to write
           outside its workspace, to find credentials in its environment, to find
           the Docker API socket, and reports its user and privileges
     WHEN: it runs in the executor
     THEN: every connection fails, so agent code can reach nothing but the broker
      AND: writes succeed in /work and /tmp and fail everywhere else, the `maji`
           module's own file included
      AND: the only maji-mcp values in its environment are the run's own token and
           the socket path, with no other credential, Docker host or service token
      AND: there is no Docker API socket, so it can't start or enter containers
      AND: it is user 10001, holds no effective capabilities, and can't gain
           privileges through setuid programs
    """
    result = run_script(PROBE)
    assert result.exit_code == 0, result.stderr
    found = json.loads(result.stdout)

    # Each probe is checked on its own, so one run shows every way out that is open.
    #
    with check:
        assert "connected" not in found["network"].values()

    with check:
        writes = dict(found["writes"])
        assert (writes.pop("/work/a"), writes.pop("/tmp/a")) == ("written", "written")
        assert "written" not in writes.values()

    # The image's git settings are named GIT_CONFIG_KEY_0 and so on: config keys,
    # not credentials. They are let through by name, one for each setting.
    #
    with check:
        names = found["environment"]
        git_settings = {f"GIT_CONFIG_KEY_{n}" for n in range(4)}
        suspicious = [
            name
            for name in names
            if any(w in name for w in ("TOKEN", "SECRET", "KEY", "DOCKER", "SENTRY"))
            and name != "MAJI_RUN_TOKEN"
            and name not in git_settings
        ]
        assert {"MAJI_RUN_TOKEN", "MAJI_BROKER_SOCKET"} <= set(names)
        assert suspicious == []

    with check:
        assert found["docker_sockets"] == []

    with check:
        assert found["process"] == {
            "uid": 10001,
            "gid": 10001,
            "cap_eff": "0000000000000000",
            "no_new_privs": "1",
        }


def test_the_memory_limit_ends_the_run(run_script: Callable[..., RunResult]) -> None:
    """
    GIVEN: A script that allocates far more memory than its limit
     WHEN: it runs in the executor
     THEN: the kernel kills it, and the run ends with a nonzero exit code
    """
    code = "blocks = [bytearray(32 * 1024 * 1024) for _ in range(20)]\nprint('done')"
    result = run_script(code, memory_mb=128)
    assert result.exit_code not in (0, None)
    assert "done" not in result.stdout


def test_the_process_limit_stops_runaway_threads(
    run_script: Callable[..., RunResult],
) -> None:
    """
    GIVEN: A script that starts threads without end
     WHEN: it runs in the executor
     THEN: starting more fails at the process limit, long before a hundred
    """
    code = """
import threading, time
started = 0
try:
    for _ in range(1000):
        threading.Thread(target=time.sleep, args=(5,), daemon=True).start()
        started += 1
except RuntimeError:
    pass
print(started)
"""
    result = run_script(code, pids=32)
    assert result.exit_code == 0, result.stderr
    assert int(result.stdout) < 100


def test_the_deadline_kills_a_run_and_leaves_no_container(
    run_script: Callable[..., RunResult], docker_runner: DockerRunner
) -> None:
    """
    GIVEN: A script that sleeps far longer than its time limit
     WHEN: it runs in the executor
     THEN: the runner kills it at the deadline and reports it as timed out
      AND: no container for the run remains
    """
    result = run_script("import time; time.sleep(60)", timeout_s=2)
    with check:
        assert (result.timed_out, result.exit_code) == (True, None)
    with check:
        assert result.duration_ms < 15_000
    with check:
        leftovers = docker_runner.client.containers.list(
            all=True, filters={"label": "maji-mcp/run_id=test-run"}
        )
        assert leftovers == []


def test_output_beyond_the_cap_is_cut(run_script: Callable[..., RunResult]) -> None:
    """
    GIVEN: A script that prints far more than the output cap
     WHEN: it runs in the executor
     THEN: the result holds only the first part, marked as truncated
    """
    result = run_script("print('x' * 200_000)", output_bytes=1024)
    assert result.truncated is True
    assert len(result.stdout) == 1024


@pytest.fixture
def stoppable(docker_api: docker.DockerClient, runtime_image: str) -> DockerRunner:
    """
    A runner of the test's own to tell to stop: a runner that has stopped starts no
    more runs, so the session's shared one can't be used for it.
    """
    return DockerRunner(SandboxSettings(images={"python": runtime_image}), docker_api)


def test_a_runner_told_to_stop_ends_the_script_in_flight_and_starts_no_more(
    stoppable: DockerRunner,
) -> None:
    """
    GIVEN: A runner with a script still running in an executor
     WHEN: the runner is told to stop
     THEN: the script's run ends at once, as one the stopping runner ended, and its
           container is removed, so no script outlives the runner that holds it to
           its deadline
      AND: a run asked for afterwards is refused the same way, and no container is
           made for it
    """
    containers = stoppable.client.containers
    label = {"label": "maji-mcp/run_id=test-stopping"}
    outcome: list[Any] = []

    # The script would sleep for a minute. Its run waits on a thread, as a run does
    # in the runner service.
    #
    def run() -> None:
        try:
            outcome.append(
                stoppable.run(
                    "python", "import time; time.sleep(60)", "test-stopping", "token"
                )
            )
        except RunnerStopping as exc:
            outcome.append(exc)

    waiting = threading.Thread(target=run)
    waiting.start()
    deadline = time.monotonic() + 30
    while not containers.list(filters=label):
        assert time.monotonic() < deadline, "the executor never started"
        time.sleep(0.1)

    asked = time.monotonic()
    stoppable.stop()
    waiting.join(20)

    with check:
        assert [type(ended) for ended in outcome] == [RunnerStopping]
    with check:
        assert time.monotonic() - asked < 10
    with check:
        assert containers.list(all=True, filters=label) == []
    with check, pytest.raises(RunnerStopping):
        stoppable.run("python", "print('ran')", "test-stopped", "token")
    with check:
        assert (
            containers.list(all=True, filters={"label": "maji-mcp/run_id=test-stopped"})
            == []
        )


def test_a_script_started_as_the_runner_stops_is_ended_too(
    stoppable: DockerRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    GIVEN: A run whose container exists and hasn't started when the runner is told
           to stop
     WHEN: the container starts
     THEN: the script is ended there and then, as one the stopping runner ended, and
           its container is removed
    """
    # The runner is told to stop from inside the step that starts the container, the
    # moment a stop has nothing running to end.
    #
    start = stoppable._send_stdin

    def stop_then_start(container: Any, code: str) -> None:
        stoppable.stop()
        start(container, code)

    monkeypatch.setattr(stoppable, "_send_stdin", stop_then_start)

    asked = time.monotonic()
    with pytest.raises(RunnerStopping):
        stoppable.run("python", "import time; time.sleep(60)", "test-racing", "token")

    with check:
        assert time.monotonic() - asked < 10
    with check:
        assert (
            stoppable.client.containers.list(
                all=True, filters={"label": "maji-mcp/run_id=test-racing"}
            )
            == []
        )


@dataclass
class LeftBehind:
    """
    Two stacks' broker socket volumes, and the executors a test leaves running as a
    killed runner would.

    Attributes:
        client: the Docker API
        image: the image executors are started from
        ours: the socket volume of the stack whose runner the test starts
        theirs: the socket volume of another stack on the same Docker daemon
        containers: the executors left so far
    """

    client: docker.DockerClient
    image: str
    ours: str
    theirs: str
    containers: list[Any] = field(default_factory=list)

    def settings(self, volume: str | None) -> SandboxSettings:
        """The settings of a runner whose broker's socket is on `volume`."""
        return SandboxSettings(images={"python": self.image}, broker_volume=volume)

    def leave(self, run_id: str, volume: str | None) -> Any:
        """
        Start a script that sleeps for two minutes in an executor, as a runner on
        `volume` starts one, and return its container without waiting on it.
        """
        config = container_config(
            self.settings(volume), "python", run_id, "token", DEFAULT_LIMITS, (), ()
        )
        container = self.client.containers.create(**config)
        self.containers.append(container)
        DockerRunner._send_stdin(container, "import time; time.sleep(120)")
        return container

    def running(self) -> set[str]:
        """The ids of the containers running now."""
        return {container.id for container in self.client.containers.list()}


@pytest.fixture
def left_behind(
    docker_api: docker.DockerClient, runtime_image: str
) -> Iterator[LeftBehind]:
    """
    Two empty volumes standing for two stacks' broker socket volumes, with a way to
    leave executors running on them. The executors and the volumes are removed
    afterwards.
    """
    suffix = uuid.uuid4().hex[:8]
    ours, theirs = (
        docker_api.volumes.create(f"maji-test-{name}-{suffix}")
        for name in ("ours", "theirs")
    )
    left = LeftBehind(docker_api, runtime_image, ours.name, theirs.name)
    try:
        yield left
    finally:
        for container in left.containers:
            with contextlib.suppress(docker.errors.NotFound):
                container.remove(force=True)
        for volume in (ours, theirs):
            volume.remove(force=True)


def test_a_runner_that_starts_removes_the_executors_an_earlier_runner_left(
    left_behind: LeftBehind, docker_runner: DockerRunner
) -> None:
    """
    GIVEN: Two stacks on one Docker daemon, each with a script still running in an
           executor whose runner was killed, and a third such executor that mounts
           no broker's socket
     WHEN: a new runner of the first stack removes what an earlier runner left, as
           it does when it starts
     THEN: the first stack's executor is removed, and the runner names its run, so
           the broker can be told the run is over
      AND: the other stack's executor and the third go on running, since a runner
           removes only what mounts its own broker's socket volume
      AND: a runner with no broker socket volume removes nothing
    """
    ours = left_behind.leave("left-ours", left_behind.ours)
    theirs = left_behind.leave("left-theirs", left_behind.theirs)
    loose = left_behind.leave("left-loose", None)
    assert {ours.id, theirs.id, loose.id} <= left_behind.running()

    starting = DockerRunner(left_behind.settings(left_behind.ours), left_behind.client)
    with check:
        assert starting.sweep() == ["left-ours"]
    with check:
        assert ours.id not in {
            c.id for c in left_behind.client.containers.list(all=True)
        }
    with check:
        assert {theirs.id, loose.id} <= left_behind.running()

    # The session's runner has no broker socket volume, so it has nothing to tell
    # its own executors from another's by.
    #
    with check:
        assert docker_runner.sweep() == []
    with check:
        assert {theirs.id, loose.id} <= left_behind.running()
