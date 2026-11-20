"""
Tests that a script in the executor reaches the broker in its own container.

The broker runs from the service image as user 10002, and the executor from the
runtime image as user 10001. They share only the call socket's named volume, which
the executor mounts read-only. These tests need both images:

    make test-docker
"""

# system imports
import json
from collections.abc import Iterator

# 3rd party imports
import docker
import pytest
from pytest_check import check

# project imports
from tests.support.containers import PRINCIPAL, Broker, start_broker

pytestmark = pytest.mark.docker

POLICY = {"principals": {PRINCIPAL: {"allow": ["maji.*"], "per_minute": 60}}}


@pytest.fixture(scope="module")
def broker(
    docker_api: docker.DockerClient, service_image: str, runtime_image: str
) -> Iterator[Broker]:
    """
    A broker with no network and no servers, whose principal may call the built-in
    `maji` stubs.

    Starting one takes seconds and no test changes it, so the module shares one. Each
    test registers a run of its own.
    """
    broker = start_broker(docker_api, service_image, runtime_image, POLICY)
    try:
        yield broker
    finally:
        broker.remove()


def test_a_script_calls_the_broker_across_containers(broker: Broker) -> None:
    """
    GIVEN: A broker in its own container, and a run registered with it
     WHEN: a script in an executor container calls maji.whoami() over the shared
           socket, then a stub that doesn't exist
     THEN: the first call succeeds, and the broker answers with the run's own
           principal and id
      AND: the second raises maji.StubError saying `not_found`: the built-in `maji`
           stubs sit beside the `maji` module's own helpers and don't replace them
    """
    run = broker.register()
    code = """
import json
answers = {"whoami": maji.whoami()}
try:
    maji.call("nothing.here")
except maji.StubError as exc:
    answers["missing"] = exc.code
print(json.dumps(answers))
"""
    try:
        result = broker.run(code, run)
    finally:
        broker.finish(run)
    assert result.exit_code == 0, result.stderr
    answers = json.loads(result.stdout)
    with check:
        assert answers["whoami"] == {"principal": PRINCIPAL, "run_id": run["run_id"]}
    with check:
        assert answers["missing"] == "not_found"


def test_a_finished_runs_token_is_refused_across_containers(broker: Broker) -> None:
    """
    GIVEN: A run that the runner has finished
     WHEN: a script in an executor presents that run's token
     THEN: the broker refuses it as unauthenticated
    """
    run = broker.register()
    broker.finish(run)
    code = """
try:
    maji.call("maji.whoami")
    print("allowed")
except maji.StubError as exc:
    print(exc.code)
"""
    result = broker.run(code, run)
    assert result.stdout.strip() == "unauthenticated"


def test_the_executor_cannot_change_the_socket_volume(broker: Broker) -> None:
    """
    GIVEN: The broker's socket volume, mounted into the executor
     WHEN: a script tries to replace the socket, or add a file beside it
     THEN: both fail, because the executor mounts the volume read-only
    """
    code = """
import json, os
results = {}
for action, path in [("remove", "/run/maji/call.sock"), ("write", "/run/maji/evil.sock")]:
    try:
        if action == "remove":
            os.remove(path)
        else:
            open(path, "w").close()
        results[action] = "done"
    except OSError as exc:
        results[action] = type(exc).__name__
print(json.dumps(results))
"""
    result = broker.run(code)
    assert result.exit_code == 0, result.stderr
    assert "done" not in json.loads(result.stdout).values()
