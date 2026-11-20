"""
Fixtures for the gateway's tests: an MCP session with the gateway, and what an agent's
app says it is.

The gateway itself, served over HTTP, is `gateway` in the root `conftest.py`.
"""

# system imports
import contextlib
from collections.abc import AsyncIterator

# 3rd party imports
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

# project imports
from tests.gateway.factories import ImplementationFactory
from tests.support.gateway import Connect


@pytest.fixture
def implementation() -> Implementation:
    """What an agent's app says it is as it connects: a made-up name and version."""
    app: Implementation = ImplementationFactory.build()
    return app


@pytest.fixture
def connected(gateway: str) -> Connect:
    """
    A function that opens an MCP session with the gateway for a token's principal, as
    a client on an earlier protocol revision does, and ends it on the way out.
    """

    # The client's streams run in a task group, which has to be entered and left in
    # one task, and a fixture is set up in one task and torn down in another. So the
    # test opens the session itself, in a block of its own.
    #
    @contextlib.asynccontextmanager
    async def connect(token: str) -> AsyncIterator[ClientSession]:
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
            streamable_http_client(gateway, http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session

    return connect
