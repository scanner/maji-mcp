"""
Tests for stopping a service on a signal (`src/maji_mcp/common/stopping.py`).

The signals are sent to the test's own process, which is safe only while
`until_stopped` has its handlers in place: each test checks that before it sends one.
"""

# system imports
import asyncio
import os
import signal
from typing import Any

# 3rd party imports
import pytest
from pytest_check import check

# project imports
from maji_mcp.common.stopping import until_stopped


@pytest.mark.parametrize(
    ("sig", "pythons_own"),
    [
        (signal.SIGTERM, signal.SIG_DFL),
        (signal.SIGINT, signal.default_int_handler),
    ],
    ids=["SIGTERM", "SIGINT"],
)
async def test_a_stop_signal_ends_the_service_after_its_cleanup(
    sig: signal.Signals, pythons_own: Any
) -> None:
    """
    GIVEN: A service running until it is cancelled, with something to clean up on
           the way out
     WHEN: its process receives SIGTERM, as Docker or a service manager sends, or
           SIGINT, as Ctrl-C sends
     THEN: the service is cancelled, its cleanup runs to the end, and the call
           returns, so the process can end by itself
      AND: once the service has stopped, the signal is Python's own to handle again
    """
    before = signal.getsignal(sig)
    cleaned: list[str] = []
    serving = asyncio.Event()

    async def service() -> None:
        try:
            serving.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.append("done")

    running = asyncio.create_task(until_stopped(service()))
    await asyncio.wait_for(serving.wait(), 5)

    # Sent without a handler in place, the signal would end the tests themselves.
    #
    assert signal.getsignal(sig) is not before
    os.kill(os.getpid(), sig)
    await asyncio.wait_for(running, 5)

    with check:
        assert cleaned == ["done"]
    with check:
        assert signal.getsignal(sig) is pythons_own


async def test_a_service_that_ends_by_itself_needs_no_signal() -> None:
    """
    GIVEN: A service whose main coroutine returns
     WHEN: it is run until stopped
     THEN: the call returns when the service does
    """
    ended: list[str] = []

    async def service() -> None:
        ended.append("returned")

    await asyncio.wait_for(until_stopped(service()), 5)
    assert ended == ["returned"]
