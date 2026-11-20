"""
Values a session's scripts keep for each other: `maji.save` and `maji.load`.

Each `run_python` script starts in a fresh container, so a script that checks an
earlier one's result would otherwise fetch its data again. Instead a script saves what
it fetched, and later scripts in the same session load it.

The broker keeps the values, so the sandbox mounts nothing new. A value belongs to the
session the runner registered the run under, which the gateway derives from the
caller's principal and its MCP session, or the session its app names
(`gateway/server.py`, `session_of`). A script can't name another session: the key
comes with the run, not with the call.

Values are kept as JSON text and parsed on load, never evaluated. Memory is bounded:
- one value: 8 MB
- one session: 32 MB
- every session together: 256 MB; past that, the least recently used sessions are
  dropped
- a session unused for an hour is dropped
"""

# system imports
import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# project imports
from maji_mcp.common.protocol import MajiError

MAX_VALUE_BYTES = 8 * 1024 * 1024
MAX_SESSION_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
IDLE_SECONDS = 3600
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


###############################################################################
#
@dataclass
class _Saved:
    text: str
    saved_at: str

    @property
    def size(self) -> int:
        return len(self.text.encode())


@dataclass
class _Session:
    values: dict[str, _Saved] = field(default_factory=dict)
    used: float = 0.0

    @property
    def size(self) -> int:
        return sum(v.size for v in self.values.values())


class SessionStore:
    """
    Named JSON values, per session, within fixed memory bounds.

    Args:
        clock: the time source for idle expiry, in seconds; replaced in tests
        max_value: the largest value, in bytes of JSON
        max_session: the most one session may hold
        max_total: the most every session together may hold
    """

    def __init__(
        self,
        clock: Callable[[], float] | None = None,
        max_value: int = MAX_VALUE_BYTES,
        max_session: int = MAX_SESSION_BYTES,
        max_total: int = MAX_TOTAL_BYTES,
    ) -> None:
        self._clock = clock or time.monotonic
        self.max_value = max_value
        self.max_session = max_session
        self.max_total = max_total
        self._sessions: OrderedDict[str, _Session] = OrderedDict()

    def save(self, session: str | None, name: str, value: Any) -> dict[str, Any]:
        """
        Keep a value under a name, replacing any value saved under it before.

        Returns:
            The name, and the value's size in bytes of JSON

        Raises:
            MajiError: `unavailable` for a run with no session, `invalid_argument` for
                a bad name, or a value that is too large or would take the session
                over its limit
        """
        held = self._session(session)
        _check_name(name)
        text = json.dumps(value)
        saved = _Saved(text, datetime.now(UTC).isoformat(timespec="seconds"))
        if saved.size > self.max_value:
            raise MajiError(
                "invalid_argument",
                f"{name}: {saved.size:,} bytes is over the {self.max_value:,}-byte "
                "limit for one value; save less, such as only the fields you need",
            )
        old = held.values.get(name)
        after = held.size - (old.size if old else 0) + saved.size
        if after > self.max_session:
            raise MajiError(
                "invalid_argument",
                f"this session would hold {after:,} bytes, over its "
                f"{self.max_session:,}-byte limit; save less, or overwrite a name",
            )
        held.values[name] = saved
        self._evict(keep=session)
        return {"name": name, "bytes": saved.size}

    def load(self, session: str | None, name: str) -> Any:
        """
        Return the value saved under a name.

        Raises:
            MajiError: `unavailable` for a run with no session, `not_found` with the
                saved names when nothing is saved under this one
        """
        held = self._session(session)
        saved = held.values.get(name)
        if saved is None:
            raise MajiError(
                "not_found",
                f"nothing saved as {name!r} in this session",
                {"saved": sorted(held.values)},
            )
        return json.loads(saved.text)

    def saved(self, session: str | None) -> list[dict[str, Any]]:
        """List this session's saved values: name, bytes and when each was saved."""
        held = self._session(session)
        return [
            {"name": name, "bytes": v.size, "saved_at": v.saved_at}
            for name, v in sorted(held.values.items())
        ]

    def _session(self, session: str | None) -> _Session:
        """Return the session's values, after dropping sessions idle too long."""
        if not session:
            raise MajiError(
                "unavailable",
                "this run has no session to keep values in: the agent's app sent "
                "neither an MCP session nor an X-Maji-Session header",
            )
        now = self._clock()
        for key in [
            k for k, s in self._sessions.items() if now - s.used > IDLE_SECONDS
        ]:
            del self._sessions[key]
        held = self._sessions.setdefault(session, _Session())
        held.used = now
        self._sessions.move_to_end(session)
        return held

    def _evict(self, keep: str | None) -> None:
        """Drop the least recently used sessions until every session fits."""
        total = sum(s.size for s in self._sessions.values())
        for key in list(self._sessions):
            if total <= self.max_total:
                return
            if key != keep:
                total -= self._sessions.pop(key).size


def _check_name(name: object) -> None:
    if not isinstance(name, str) or not NAME.match(name):
        raise MajiError(
            "invalid_argument",
            "a name is 1 to 64 letters, digits, dots, dashes or underscores",
        )
