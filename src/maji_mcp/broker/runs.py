"""
Runs in progress, and the token that identifies each one to the broker.

The executor is untrusted, and every run shares the one call socket, so a run can't be
believed about who it is. Instead the runner, which is trusted, registers each run on
the broker's control socket and receives a random token for it. The token is the only
secret the executor holds. The broker finds the run, and so the principal, from the
token, and ignores anything else a request says about identity. When the run ends, the
runner finishes it and the token stops working.

This is the control plane's split between facts and claims, applied to identity: the
principal is a fact the broker recorded, never a claim the script makes.
"""

# system imports
import asyncio
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

# project imports
from maji_mcp.common.protocol import MajiError

# A run's token stops working after this long even if the runner never finishes it,
# so a token leaked by a crash doesn't stay valid.
#
MAX_RUN_SECONDS = 3600

# How many of one run's stub calls the broker works on at once. A script may call from
# many threads; past this many, its calls wait their turn, so one run can't flood a
# service.
#
MAX_CALLS_IN_FLIGHT = 8


###############################################################################
#
@dataclass
class Run:
    """One run in progress."""

    run_id: str
    principal: str
    runtime: str
    session: str | None = None
    started: float = field(default_factory=time.monotonic)
    calls: int = 0
    reported: bool = False
    slots: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(MAX_CALLS_IN_FLIGHT)
    )


class RunRegistry:
    """
    Maps each live run's token to the run.

    Args:
        clock: the time source, in seconds; replaced in tests
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._by_token: dict[str, Run] = {}

    def register(
        self, principal: str, runtime: str, session: str | None = None
    ) -> tuple[Run, str]:
        """Start a run, and return it with its token."""
        run = Run(uuid.uuid4().hex, principal, runtime, session, started=self._clock())
        token = secrets.token_urlsafe(32)
        self._by_token[token] = run
        return run, token

    def lookup(self, token: object) -> Run:
        """
        Return the live run a token belongs to.

        Raises:
            MajiError: `unauthenticated` for a missing, unknown, finished or expired
                token. The message is the same for each, so it reveals nothing.
        """
        run = self._by_token.get(token) if isinstance(token, str) else None
        if run is None or self._clock() - run.started > MAX_RUN_SECONDS:
            raise MajiError("unauthenticated", "no live run for this token")
        return run

    def finish(self, run_id: str) -> Run | None:
        """End a run, so its token stops working. Returns the run, if it was live."""
        for token, run in list(self._by_token.items()):
            if run.run_id == run_id:
                del self._by_token[token]
                return run
        return None

    def __len__(self) -> int:
        return len(self._by_token)
