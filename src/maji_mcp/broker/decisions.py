"""
The broker's side of the decision feed: your decisions, applied in the order you made
them.

The control plane records each decision and keeps them in order
(`control/internal.py`, `decisions_since`). The broker reads those after the last
position it applied, hands each to the appliers that know its kind of item, records
what applying it did (`record_outcome`), and keeps the new position in a file, so a
restart neither misses a decision nor applies one twice. Appliers are also safe to
run twice on the same decision: each checks its own records first.

The feed is read every few seconds, and again whenever something is about to depend
on it, such as a script calling a skill or an agent listing them, so a decision you
have just made holds from the next call on. Reading it is one request over a local
socket.
"""

# system imports
import asyncio
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

# project imports
from maji_mcp.common.protocol import MajiError, call_unix

Applier = Callable[[dict[str, Any], dict[str, Any]], Awaitable[tuple[str, str] | None]]

BATCH = 100


###############################################################################
#
class DecisionFeed:
    """
    Reads your decisions from the control plane and applies them.

    Args:
        control_plane: the control plane's socket for the broker
        position_file: where the last position applied is kept
        appliers: each is given a decision and its item, and returns the outcome
            to record, or None for an item it doesn't apply
        clock: seconds, for how fresh the feed is
    """

    def __init__(
        self,
        control_plane: Path,
        position_file: Path,
        appliers: list[Applier],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.control_plane = control_plane
        self.position_file = position_file
        self.appliers = appliers
        self.clock = clock
        self._lock = asyncio.Lock()
        self._read_at: float | None = None

    def position(self) -> int:
        try:
            return int(self.position_file.read_text().strip() or 0)
        except FileNotFoundError:
            return 0

    def _keep(self, position: int) -> None:
        fd, temp = tempfile.mkstemp(dir=self.position_file.parent)
        with os.fdopen(fd, "w") as out:
            out.write(f"{position}\n")
        os.replace(temp, self.position_file)

    async def catch_up(self) -> int:
        """
        Apply every decision after the position kept, and return how many there were.

        Raises:
            OSError: the control plane can't be reached
            MajiError: it refused a request
        """
        async with self._lock:
            applied = 0
            position = self.position()
            while True:
                found = await call_unix(
                    self.control_plane,
                    "decisions_since",
                    {"position": position, "limit": BATCH},
                )
                for entry in found["decisions"]:
                    await self._apply(entry["decision"], entry["item"])
                    position = int(entry["position"])
                    self._keep(position)
                    applied += 1
                if len(found["decisions"]) < BATCH:
                    break
            self._read_at = self.clock()
            return applied

    async def _apply(self, decision: dict[str, Any], item: dict[str, Any]) -> None:
        for applier in self.appliers:
            try:
                outcome = await applier(decision, item)
            except Exception as exc:  # recorded as failed, and the feed goes on
                outcome = ("failed", f"{type(exc).__name__}: {exc}")
            if outcome is not None:
                await call_unix(
                    self.control_plane,
                    "record_outcome",
                    {
                        "decision": decision["id"],
                        "outcome": outcome[0],
                        "detail": outcome[1],
                    },
                )

    async def fresh(self, within: float = 0.0) -> None:
        """
        Catch up, unless the feed was read within the last `within` seconds. A
        control plane that can't be reached leaves things as they are.
        """
        if self._read_at is not None and self.clock() - self._read_at < within:
            return
        try:
            await self.catch_up()
        except (OSError, MajiError) as exc:
            print(f"broker: decisions not read: {type(exc).__name__}", flush=True)

    async def run(self, every: float = 3.0) -> None:
        """Catch up every few seconds, for good."""
        while True:
            try:
                await self.fresh(within=0.0)
            except Exception as exc:  # reported, and read again next time
                print(f"broker: decisions not applied: {exc!r}", flush=True)
            await asyncio.sleep(every)
