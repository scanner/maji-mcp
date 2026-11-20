"""
Identifiers: an instance's, from its host's name, and an item's, from that and a ULID.

An instance id is fixed when the instance is created, because it goes into item ids
and so into signed hashes (`api/openapi.yaml`, `InstanceIdentifier`). An item id is
the instance id, a hyphen, and a ULID in lower case: a 48-bit time in milliseconds
and 80 random bits, written in Crockford's base 32, which sorts by time.
"""

# system imports
import re
import secrets
import time

CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def instance_id(hostname: str, taken: set[str] | frozenset[str] = frozenset()) -> str:
    """
    Make an instance id from a host's name: its first label, in lower case.

    Characters a DNS label can't hold become hyphens. A name already taken on this
    host gets `-2`, `-3` and so on.

    Args:
        hostname: the host's name, such as `studio-mac.example.net`
        taken: the ids of the instances already on this host
    """
    label = hostname.split(".", 1)[0].lower()
    label = re.sub(r"[^a-z0-9-]+", "-", label)
    label = re.sub(r"-{2,}", "-", label).strip("-")[:59].strip("-") or "instance"
    candidate, n = label, 1
    while candidate in taken:
        n += 1
        candidate = f"{label}-{n}"
    return candidate


def ulid(now_ms: int | None = None) -> str:
    """A ULID in lower case: 26 characters that sort by the time they were made."""
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    value = (ms << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        chars.append(CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def item_id(instance: str) -> str:
    """A new item id for an item this instance owns."""
    return f"{instance}-{ulid()}"
