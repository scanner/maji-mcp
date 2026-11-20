"""
Tests that a broker in a container reaches a server that acts as you, on the host.

The bridge serves a fake stdio MCP server on this machine's 127.0.0.1. The broker runs
in a container and reaches it through `host.docker.internal`, the path
`docs/design/server-contract.md` B2 describes for a Mac. A script in an executor then
reads through the whole chain.
"""

# system imports
import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

# 3rd party imports
import docker
import pytest

# project imports
from tests.support.bridge import Bridged, serving_bridge
from tests.support.containers import PRINCIPAL, Broker, start_broker
from tests.support.paths import FAKE_SERVER

pytestmark = pytest.mark.docker

POLICY = {"principals": {PRINCIPAL: {"allow": ["fake.*"], "per_minute": 60}}}


@pytest.fixture
async def bridge(tmp_path: Path, unused_tcp_port: int) -> AsyncIterator[Bridged]:
    """The bridge serving the fake server on this machine's 127.0.0.1."""
    command = [sys.executable, str(FAKE_SERVER)]
    async with serving_bridge("fake", command, unused_tcp_port, tmp_path) as bridge:
        yield bridge


@pytest.fixture
async def broker(
    bridge: Bridged,
    docker_api: docker.DockerClient,
    service_image: str,
    runtime_image: str,
) -> AsyncIterator[Broker]:
    """
    A broker container configured to expose one read tool of the bridged server.

    The broker lists the server's tools as it starts, through the bridge, so it is
    started in a thread while the event loop goes on serving the bridge.
    """
    servers = {
        "servers": {
            "fake": {
                "url": f"http://host.docker.internal:{bridge.port}/mcp",
                "token_file": "/tmp/fake.token",
                "tools": {"get_thing": "read"},
            }
        }
    }
    broker = await asyncio.to_thread(
        start_broker,
        docker_api,
        service_image,
        runtime_image,
        POLICY,
        servers=servers,
        files={"fake.token": bridge.token},
        networked=True,
    )
    try:
        yield broker
    finally:
        await asyncio.to_thread(broker.remove)


async def test_a_script_reads_a_host_server_through_the_broker(broker: Broker) -> None:
    """
    GIVEN: A server that acts as you, bridged on this machine's 127.0.0.1, and a broker
           container configured to expose one of its read tools
     WHEN: a script in an executor calls that stub
     THEN: the call crosses from the executor to the broker container to the host,
           and the script gets the server's answer
    """
    assert "broker: fake: 1 stubs" in broker.logs(), broker.logs()

    # The script's call comes back through the bridge, which this event loop serves,
    # so the run waits in a thread.
    #
    result = await asyncio.to_thread(
        broker.run, "print(fake.get_thing(key='host')['value'])"
    )
    assert result.exit_code == 0, result.stderr + broker.logs()
    assert result.stdout.strip() == "value-of-host"
