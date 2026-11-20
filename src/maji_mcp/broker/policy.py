"""
Who may call what, and how often: RBAC v0 and rate limits.

The policy is a static JSON file, mapping each principal to the stubs it may call:

    {
      "principals": {
        "claude-code": {"allow": ["sentry.*"], "per_minute": 120},
        "advisor": {"allow": ["sentry.projects", "sentry.issues"], "per_minute": 30}
      }
    }

`allow` holds shell-style patterns over stub names. A principal not in the file may do
nothing. Grants from the control plane are planned to replace this file
(`docs/roadmap.md`).

Rate limits are a token bucket per principal and namespace, so one busy script can't
exhaust a service's allowance for every other caller, or for itself in another
namespace.
"""

# system imports
import fnmatch
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# project imports
from maji_mcp.common.protocol import MajiError


###############################################################################
#
@dataclass(frozen=True)
class Grant:
    """One principal's permissions."""

    allow: tuple[str, ...]
    per_minute: int = 120


@dataclass
class Policy:
    """The principals the broker knows, and what each may call."""

    grants: dict[str, Grant] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: Path) -> Policy:
        data = json.loads(path.read_text())
        grants = {
            name: Grant(
                allow=tuple(entry.get("allow", [])),
                per_minute=int(entry.get("per_minute", 120)),
            )
            for name, entry in data.get("principals", {}).items()
        }
        return cls(grants)

    def knows(self, principal: str) -> bool:
        return principal in self.grants

    def allows(self, principal: str, stub: str) -> bool:
        grant = self.grants.get(principal)
        if grant is None:
            return False
        return any(fnmatch.fnmatchcase(stub, pattern) for pattern in grant.allow)

    def per_minute(self, principal: str) -> int:
        return self.grants[principal].per_minute


###############################################################################
#
@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """
    A token bucket per principal and namespace.

    Each bucket holds up to a minute's allowance and refills continuously, so a burst
    up to the allowance is fine, and a steady rate above it is not.

    Args:
        clock: the time source, in seconds; replaced in tests
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._buckets: dict[tuple[str, str], _Bucket] = {}

    def take(self, principal: str, namespace: str, per_minute: int) -> None:
        """
        Spend one call from the bucket.

        Raises:
            MajiError: `rate_limited`, with how many seconds until a call is allowed
        """
        now = self._clock()
        key = (principal, namespace)
        bucket = self._buckets.setdefault(key, _Bucket(float(per_minute), now))
        rate = per_minute / 60.0
        bucket.tokens = min(per_minute, bucket.tokens + (now - bucket.updated) * rate)
        bucket.updated = now
        if bucket.tokens < 1:
            wait = (1 - bucket.tokens) / rate
            raise MajiError(
                "rate_limited",
                f"{principal} is over {per_minute} {namespace} calls a minute",
                {"retry_after_s": round(wait, 1)},
            )
        bucket.tokens -= 1
