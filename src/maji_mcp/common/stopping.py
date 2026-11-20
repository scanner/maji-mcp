"""
Stopping a service when it is told to: on SIGTERM, which Docker and service managers
send, or SIGINT, which Ctrl-C sends.

A service is the first process in its container. The kernel delivers a signal to that
process only if the process handles it, and Python handles SIGTERM only when told to,
so a service that didn't would be left running until Docker gave up on it and killed
it. `until_stopped` turns either signal into the cancellation of the service's main
coroutine, so whatever the service cleans up in a `finally` is cleaned up, and the
process ends by itself.
"""

# system imports
import asyncio
import contextlib
import signal
from collections.abc import Coroutine
from typing import Any

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


async def until_stopped(main: Coroutine[Any, Any, None]) -> None:
    """
    Run a service until it ends or is told to stop, by SIGTERM or SIGINT.

    Either signal cancels `main`, and the call returns once `main` has finished
    whatever it does on the way out. A second signal cancels that too.

    Args:
        main: the service's main coroutine, which runs until cancelled
    """
    task = asyncio.ensure_future(main)
    loop = asyncio.get_running_loop()
    for sig in STOP_SIGNALS:
        loop.add_signal_handler(sig, task.cancel)
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        for sig in STOP_SIGNALS:
            loop.remove_signal_handler(sig)
