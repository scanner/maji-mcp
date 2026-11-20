"""
Fixtures for the tests that need a Docker daemon and maji-mcp's two images.

`tests/docker/__init__.py` says which images are tested, and how `make test-docker`
names them. A test asks for what it needs: `runtime_image`, `service_image`, or a
fixture built on them. Whatever is missing skips the test rather than failing it.
"""

# system imports
import gc
from collections.abc import Callable, Iterator, Sequence

# 3rd party imports
import docker
import pytest

# project imports
from maji_mcp.runner.docker_runner import DockerRunner, RunResult, docker_client
from maji_mcp.runner.sandbox import Cover, Limits, Mount, SandboxSettings
from tests.docker import RUNTIME_IMAGE, SERVICE_IMAGE


def _present(client: docker.DockerClient, image: str) -> str:
    """Return `image`, or skip the test if the daemon doesn't have it."""
    try:
        client.images.get(image)
    except Exception as exc:  # noqa: BLE001 - any failure means nothing to test on
        pytest.skip(f"no image {image}: {exc}")
    return image


@pytest.fixture(autouse=True)
def collected() -> Iterator[None]:
    """
    Garbage collected after each test.

    The Docker SDK leaves the HTTP response behind a container's attached socket to
    the collector (`DockerRunner._send_stdin`). Collected late, after its connection
    has gone, the response fails to flush and pytest reports that against whichever
    test happens to be running. Collecting after each test finalizes it while it
    still can.
    """
    yield
    gc.collect()


@pytest.fixture(scope="session")
def docker_api() -> docker.DockerClient:
    """A client for the Docker API, found as the runner finds it."""
    try:
        client = docker_client()
        client.ping()
    except Exception as exc:  # noqa: BLE001 - any failure means no Docker to test on
        pytest.skip(f"no Docker daemon: {exc}")
    return client


@pytest.fixture(scope="session")
def runtime_image(docker_api: docker.DockerClient) -> str:
    """The name of the image executors are started from."""
    return _present(docker_api, RUNTIME_IMAGE)


@pytest.fixture(scope="session")
def service_image(docker_api: docker.DockerClient) -> str:
    """The name of the image the broker, runner, gateway and control plane run from."""
    return _present(docker_api, SERVICE_IMAGE)


@pytest.fixture(scope="session")
def docker_runner(docker_api: docker.DockerClient, runtime_image: str) -> DockerRunner:
    """
    A runner that starts executors from the runtime image, with no broker to reach.

    One serves the whole session: it holds only its settings and the client, and every
    run it starts is a container of its own.
    """
    return DockerRunner(SandboxSettings(images={"python": runtime_image}), docker_api)


@pytest.fixture
def run_script(docker_runner: DockerRunner) -> Callable[..., RunResult]:
    """
    A function that runs a script in a fresh executor and returns what the run did.

    `mounts` are bound as granted and `covers` put in place. Any other keyword is one
    of the run's limits, such as `timeout_s=2`.
    """

    def run(
        code: str,
        *,
        mounts: Sequence[Mount] = (),
        covers: Sequence[Cover] = (),
        **limits: int,
    ) -> RunResult:
        return docker_runner.run(
            "python",
            code,
            "test-run",
            "test-token",
            Limits(**limits),
            mounts,
            {mount.source for mount in mounts},
            covers,
        )

    return run
