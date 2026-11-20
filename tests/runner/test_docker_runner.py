"""Tests for how the runner finds the Docker daemon."""

# system imports
from types import SimpleNamespace

# 3rd party imports
import pytest

# project imports
from maji_mcp.runner import docker_runner

CONTEXT_SOCKET = "unix:///home/someone/.docker/run/docker.sock"


@pytest.mark.parametrize(
    ("docker_host", "context", "expected"),
    [
        pytest.param(
            "unix:///tmp/other.sock",
            SimpleNamespace(Host=CONTEXT_SOCKET),
            "unix:///tmp/other.sock",
            id="DOCKER_HOST is set",
        ),
        pytest.param(
            None,
            SimpleNamespace(Host=CONTEXT_SOCKET),
            CONTEXT_SOCKET,
            id="the current context names a daemon",
        ),
        pytest.param(
            None,
            SimpleNamespace(Host=None),
            docker_runner.STANDARD_SOCKET,
            id="the current context names no address",
        ),
        pytest.param(
            None,
            None,
            docker_runner.STANDARD_SOCKET,
            id="the current context can't be read",
        ),
    ],
)
def test_the_daemon_is_the_one_the_docker_cli_would_use(
    monkeypatch: pytest.MonkeyPatch,
    docker_host: str | None,
    context: SimpleNamespace | None,
    expected: str,
) -> None:
    """
    GIVEN: A machine whose Docker CLI has a current context, and may have
           `DOCKER_HOST` set
     WHEN: docker_client connects to the Docker daemon
     THEN: If `DOCKER_HOST` is set, the client connects to the address it names
      AND: If `DOCKER_HOST` is unset, the client connects to the address the current
           context names
      AND: If the current context names no address, or can't be read, the client
           connects to the standard socket
    """
    # The Docker SDK is replaced on both sides: its context lookup answers with the
    # case's context, and its client records the address it was given, so no daemon
    # is needed.
    #
    if docker_host is None:
        monkeypatch.delenv("DOCKER_HOST", raising=False)
    else:
        monkeypatch.setenv("DOCKER_HOST", docker_host)
    monkeypatch.setattr(
        docker_runner.ContextAPI, "get_current_context", lambda: context
    )
    connected: list[str] = []
    monkeypatch.setattr(
        docker_runner.docker,
        "DockerClient",
        lambda base_url: connected.append(base_url),
    )

    docker_runner.docker_client()

    assert connected == [expected]
