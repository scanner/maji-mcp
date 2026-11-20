"""
Run one script in a fresh executor container, through the Docker API.

The script goes in on standard input, so nothing is written to a shared volume and no
run can see another's source. The container is created, attached, started, sent the
script, waited on with a deadline, read, and removed, whatever happens along the way.

A script is held to its deadline by the runner that waits on it, so no executor may
outlive its runner: a runner told to stop ends every run in flight first (`stop`), and
a runner that starts removes the executors an earlier runner couldn't (`sweep`).
"""

# system imports
import contextlib
import os
import socket
import threading
import time
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

# 3rd party imports
import docker
import requests
from docker.context import ContextAPI

# project imports
from maji_mcp.runner.sandbox import (
    DEFAULT_LIMITS,
    Cover,
    Limits,
    Mount,
    SandboxSettings,
    container_config,
    validate,
)

STDERR_TAIL = 8 * 1024

STANDARD_SOCKET = "unix:///var/run/docker.sock"

# The label every executor carries, whose value is its run's id (`sandbox.py`).
#
RUN_LABEL = "maji-mcp/run_id"


class CoverLost(Exception):
    """A container with covers didn't start: a path to cover has gone or changed."""


class RunnerStopping(Exception):
    """
    The runner is stopping: the script wasn't started, or was ended before it
    finished.
    """


###############################################################################
#
@dataclass
class RunResult:
    """What one run produced."""

    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
        }


def docker_client() -> docker.DockerClient:
    """
    Connect to the Docker daemon the Docker CLI would use.

    `DOCKER_HOST` names the daemon when it is set. Otherwise the daemon is the one
    the CLI's current context names, such as Rancher Desktop's or Docker Desktop's.
    Where no context is set, as in the runner's container, it is the standard
    socket. `docker.DockerClient` takes an address and leaves contexts to its
    caller, so the context is read here.
    """
    host = os.environ.get("DOCKER_HOST")
    if not host:
        context = ContextAPI.get_current_context()
        host = (context.Host if context else None) or STANDARD_SOCKET
    return docker.DockerClient(base_url=host)


###############################################################################
#
class DockerRunner:
    """
    Runs scripts in executor containers.

    Args:
        settings: the runtime allowlist and the broker's socket volume
        client: a Docker client; one is made from the environment if not given
    """

    def __init__(
        self, settings: SandboxSettings, client: docker.DockerClient | None = None
    ) -> None:
        self.settings = settings
        self.client = client or docker_client()

        # The executors of the runs in flight, by container id, for `stop` to end.
        # Each run is on a thread of its own, so a lock keeps the table.
        #
        self._live: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._stopping = threading.Event()

    def sweep(self) -> list[str]:
        """
        Remove the executors an earlier runner left, and return their runs' ids.

        A runner that crashed or was killed outright couldn't end its scripts, so an
        executor of its may still be running with nothing to hold it to a deadline.
        A runner calls this once as it starts, before it has runs of its own.

        An executor is this runner's to remove if it mounts the same broker socket
        volume, which is what tells one stack's executors from another's on the same
        Docker daemon. Without a broker volume there is nothing to tell them by, so
        nothing is removed.

        Returns:
            The id of each run whose executor was removed, for the broker to be told
            the run is over.
        """
        volume = self.settings.broker_volume
        if not volume:
            return []
        left = self.client.containers.list(
            all=True, filters={"label": RUN_LABEL, "volume": volume}
        )
        runs = []
        for container in left:
            with contextlib.suppress(docker.errors.NotFound):
                container.remove(force=True)
            runs.append(str(container.labels.get(RUN_LABEL, "")))
        return runs

    def stop(self) -> None:
        """
        End every run in flight, and start no more.

        Each run's executor is killed, so the thread waiting on it removes the
        container and raises `RunnerStopping`, as does any run asked for from now on.
        """
        self._stopping.set()
        with self._lock:
            live = list(self._live.values())
        for container in live:
            self._kill(container)

    @staticmethod
    def _kill(container: Any) -> None:
        """Kill an executor, if it is running."""
        with contextlib.suppress(docker.errors.APIError):
            container.kill()

    def run(
        self,
        runtime: str,
        code: str,
        run_id: str,
        token: str,
        limits: Limits = DEFAULT_LIMITS,
        mounts: Sequence[Mount] = (),
        granted: Collection[str] = (),
        covers: Sequence[Cover] = (),
    ) -> RunResult:
        """
        Run `code` in a new container, and remove the container afterwards.

        `mounts` are the paths to bind, and `granted` the paths the broker granted
        the run. They come from separate places, so that a path the broker didn't
        grant is refused by `validate` before any container exists. `covers` are the
        paths inside the mounts that the script reads as empty.

        Returns:
            The exit code (None if it was killed at the deadline), stdout up to
            `limits.output_bytes`, the end of stderr, and how long it took.

        Raises:
            CoverLost: the container didn't start with covers to put in place, as
                when a covered file was removed after it was found. Nothing ran.
            RunnerStopping: the runner was told to stop before the script finished.
        """
        config = container_config(
            self.settings, runtime, run_id, token, limits, mounts, covers
        )
        validate(config, self.settings, granted)
        if self._stopping.is_set():
            raise RunnerStopping()
        started = time.monotonic()
        container = self.client.containers.create(**config)
        with self._lock:
            self._live[container.id] = container
        try:
            try:
                self._send_stdin(container, code)
            except docker.errors.APIError as exc:
                # A cover is mounted on the path it hides, inside a read-only mount,
                # so a path that has gone since it was found can't be made for it,
                # and the container doesn't start. Without covers, a failed start is
                # the daemon's own trouble, and is raised as it came.
                #
                if covers:
                    raise CoverLost() from exc
                raise

            # `stop` kills the executors it finds running. One created before `stop`
            # looked and started after it is killed here, now that it runs.
            #
            if self._stopping.is_set():
                self._kill(container)
            timed_out = False
            exit_code: int | None
            try:
                status = container.wait(timeout=limits.timeout_s)
                exit_code = int(status.get("StatusCode", -1))
            except requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError:
                container.kill()
                timed_out, exit_code = True, None
            if self._stopping.is_set():
                raise RunnerStopping()
            duration_ms = int((time.monotonic() - started) * 1000)
            stdout = container.logs(stdout=True, stderr=False)
            stderr = container.logs(stdout=False, stderr=True)
        finally:
            with self._lock:
                self._live.pop(container.id, None)
            container.remove(force=True)

        truncated = len(stdout) > limits.output_bytes
        return RunResult(
            exit_code=exit_code,
            stdout=stdout[: limits.output_bytes].decode(errors="replace"),
            stderr=stderr[-STDERR_TAIL:].decode(errors="replace"),
            duration_ms=duration_ms,
            timed_out=timed_out,
            truncated=truncated,
        )

    @staticmethod
    def _send_stdin(container: Any, code: str) -> None:
        """
        Start the container and send it the script on standard input.

        The container is created with stdin open for one attach, so closing our side
        of the attached socket gives the script runner its end of file.
        """
        attached = container.attach_socket(params={"stdin": 1, "stream": 1})

        # The SDK wraps the raw socket in a `SocketIO`; writing and half-closing need
        # the socket underneath it. The wrapper is left for the SDK to close: closing
        # it here leaves its HTTP response flushing a closed file when collected.
        #
        raw: socket.socket = attached._sock
        container.start()
        raw.sendall(code.encode())
        raw.shutdown(socket.SHUT_WR)
