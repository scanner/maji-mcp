"""
A broker in a container of its own, for the tests that cross container boundaries.

The broker runs from the service image as it does in the stack, with its two sockets
on named volumes. The control socket lives in the container VM, out of reach of a
socket on this machine, so a test's control calls run inside the broker's container.
"""

# system imports
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# 3rd party imports
import docker

# project imports
from maji_mcp.runner.docker_runner import DockerRunner, RunResult
from maji_mcp.runner.sandbox import SandboxSettings

# The principal every run in these tests acts as.
#
PRINCIPAL = "tester"

# One call on the broker's control socket, run with the service image's own Python.
#
CONTROL = """
import asyncio, json, sys
from pathlib import Path
from maji_mcp.common.protocol import call_unix
method, params = sys.argv[1], json.loads(sys.argv[2])
print(json.dumps(asyncio.run(call_unix(Path("/run/maji-control/control.sock"), method, params))))
"""


@dataclass
class Broker:
    """
    A broker container, with a runner whose executors share its call socket.

    Attributes:
        container: the broker's container
        volumes: the named volumes holding its call and control sockets
        runner: starts executors that mount the call socket's volume read-only
    """

    container: Any
    volumes: list[Any]
    runner: DockerRunner

    def control(self, method: str, **params: Any) -> Any:
        """Make one call on the broker's control socket, and return its result."""
        code, output = self.container.exec_run(
            ["python", "-c", CONTROL, method, json.dumps(params)]
        )
        assert code == 0, output.decode()
        return json.loads(output)

    def register(self) -> dict[str, str]:
        """Register a run for `PRINCIPAL`, and return its `run_id` and `token`."""
        run: dict[str, str] = self.control(
            "register_run", principal=PRINCIPAL, runtime="python"
        )
        return run

    def finish(self, run: Mapping[str, str]) -> None:
        """Tell the broker the run is over, which ends its token."""
        self.control("finish_run", run_id=run["run_id"], exit_code=0, duration_ms=1)

    def run(self, code: str, run: Mapping[str, str] | None = None) -> RunResult:
        """
        Run a script in an executor that reaches this broker.

        Args:
            code: the script
            run: a run registered already, which is left unfinished. Without one, a
                run is registered for the script and finished after it.

        Returns:
            What the run did.
        """
        own = run is None
        run = self.register() if run is None else run
        try:
            return self.runner.run("python", code, run["run_id"], run["token"])
        finally:
            if own:
                self.finish(run)

    def logs(self) -> str:
        """Everything the broker has logged so far."""
        logged: bytes = self.container.logs()
        return logged.decode()

    def remove(self) -> None:
        """Remove the container and both volumes."""
        self.container.remove(force=True)
        for volume in self.volumes:
            volume.remove(force=True)


def start_broker(
    client: docker.DockerClient,
    service_image: str,
    runtime_image: str,
    policy: Mapping[str, Any],
    *,
    servers: Mapping[str, Any] | None = None,
    files: Mapping[str, str] | None = None,
    networked: bool = False,
) -> Broker:
    """
    Start a broker in a container, and return it once it is serving.

    The call blocks until the broker says it is serving. A broker that reads a server
    on this machine at start-up needs that server answering meanwhile, so a test
    whose event loop serves it calls this in a thread.

    Args:
        client: the Docker API
        service_image: the image the broker runs from
        runtime_image: the image its runner starts executors from
        policy: the broker's policy file, as a document
        servers: its servers file, if it reads any
        files: other files the broker reads, as name to content, written to `/tmp`
        networked: whether the container has a network. Without one it can reach
            nothing but its sockets, which is all a broker with no servers needs.

    Returns:
        The broker. `Broker.remove` removes everything that was created for it.
    """
    written = {"policy.json": json.dumps(policy), **(files or {})}
    command = "maji-broker --policy /tmp/policy.json --run-log /tmp/runs.db"
    if servers is not None:
        written["servers.json"] = json.dumps(servers)
        command += " --servers /tmp/servers.json"

    # The image has no test files in it, so each file's content rides in an
    # environment variable and the container's shell writes it before the broker
    # starts.
    #
    environment = {f"FILE_{n}": content for n, content in enumerate(written.values())}
    writes = [
        f'printf "%s" "$FILE_{n}" > /tmp/{name}' for n, name in enumerate(written)
    ]

    suffix = uuid.uuid4().hex[:8]
    volumes = [
        client.volumes.create(f"maji-test-{name}-{suffix}")
        for name in ("call", "control")
    ]
    container: Any = None
    try:
        container = client.containers.run(
            service_image,
            ["sh", "-c", " && ".join([*writes, f"exec {command}"])],
            environment=environment,
            mounts=[
                {"Target": "/run/maji", "Source": volumes[0].name, "Type": "volume"},
                {
                    "Target": "/run/maji-control",
                    "Source": volumes[1].name,
                    "Type": "volume",
                },
            ],
            network_mode="bridge" if networked else "none",
            detach=True,
            name=f"maji-test-broker-{suffix}",
        )
        runner = DockerRunner(
            SandboxSettings(
                broker_volume=volumes[0].name, images={"python": runtime_image}
            ),
            client,
        )
        broker = Broker(container, volumes, runner)
        deadline = time.monotonic() + 30
        while "serving" not in broker.logs():
            assert time.monotonic() < deadline, broker.logs()
            time.sleep(0.2)
        return broker
    except BaseException:
        if container is not None:
            container.remove(force=True)
        for volume in volumes:
            volume.remove(force=True)
        raise
