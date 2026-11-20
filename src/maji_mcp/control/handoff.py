"""
Keys pasted into Connect items, on their way from your browser to the broker.

The broker keeps a service's key in its credential store, and the control plane only
passes the key on. `POST /decisions` brings the key with the Connect decision you
signed. The control plane holds the key in memory, by that decision's id, until the
broker takes it while applying the decision (`internal.py`, `take_credential`), for
`HOLD_SECONDS` at most.

Nothing here reaches the database, a backup, the event stream or a log, so a key the
broker never takes, as when the control plane restarts first, is gone. The broker
then records that the key didn't reach it, and you connect the service again.
"""

# system imports
import time
from collections.abc import Callable

# How long a key waits for the broker. The broker reads decisions every few seconds,
# so a key waits that long, or until the broker starts again.
#
HOLD_SECONDS = 600.0


class KeyHandoff:
    """
    Pasted keys waiting for the broker, by decision id, each taken once.

    Args:
        clock: seconds, for how long a key has waited
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._held: dict[str, tuple[str, float]] = {}

    def __repr__(self) -> str:
        return f"KeyHandoff({len(self._held)} held)"

    def __len__(self) -> int:
        return len(self._held)

    def hold(self, decision_id: str, key: str) -> None:
        """Keep a key for the broker to take while it applies the decision."""
        self._forget_old()
        self._held[decision_id] = (key, self._clock())

    def take(self, decision_id: str) -> str | None:
        """
        Return a decision's key, and forget it.

        Returns:
            The key, or None when there is none: it was taken already, waited too
            long, or never reached this process.
        """
        self._forget_old()
        held = self._held.pop(decision_id, None)
        return held[0] if held else None

    def _forget_old(self) -> None:
        now = self._clock()
        for decision_id, (_, at) in list(self._held.items()):
            if now - at > HOLD_SECONDS:
                del self._held[decision_id]
