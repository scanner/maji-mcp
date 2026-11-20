"""
Canonical JSON and the hashes a decision is bound to (`api/openapi.yaml`, "The
decision hash").

A decision is signed over hashes of what you saw, and another instance, or a client
in a browser, has to compute the same hashes from the same JSON. So the JSON is put in
the one form RFC 8785 defines before it is hashed: object keys sorted, no whitespace,
strings escaped one way only.

Values that RFC 8785 would have to format specially are refused instead: floats, and
integers past what a JavaScript number holds exactly. Nothing maji-mcp hashes needs
them, and refusing them keeps every client's hash the same.
"""

# system imports
import base64
import hashlib
import json
from typing import Any

# The fields of an item that its content hash leaves out: its state and bookkeeping,
# which change without changing what you decide on, and its session's name, worked
# out from the session it covers. The contract lists the same ones as
# `x-maji-content-hash-excludes`, and a test keeps the two equal.
#
CONTENT_HASH_EXCLUDES = frozenset(
    {
        "state",
        "priority",
        "blocking",
        "notes",
        "actions",
        "expires_at",
        "decision",
        "superseded",
        "ask_again",
        "content_hash",
        "session_name",
    }
)

MAX_SAFE_INTEGER = 2**53 - 1


class NotCanonical(ValueError):
    """A value has no canonical form maji-mcp will hash."""


def canonical(value: Any) -> str:
    """
    Return `value` as RFC 8785 canonical JSON.

    Raises:
        NotCanonical: for a float, an integer beyond ±(2**53 − 1), a string holding a
            lone surrogate, a key that isn't a string, or a type JSON doesn't have
    """
    parts: list[str] = []
    _write(value, parts)
    return "".join(parts)


def _write(value: Any, out: list[str]) -> None:
    # `bool` is a subclass of `int`, so it is checked first.
    #
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise NotCanonical(f"integer {value} is beyond what a JSON number holds")
        out.append(str(value))
    elif isinstance(value, float):
        raise NotCanonical("floats aren't hashed; send the value as a string")
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, list | tuple):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _write(item, out)
        out.append("]")
    elif isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise NotCanonical("object keys must be strings")

        # RFC 8785 orders keys by their UTF-16 code units, as JavaScript does. Comparing
        # the UTF-16-BE bytes gives that order; sorting the Python strings directly
        # would order by code point, which differs for characters past U+FFFF.
        #
        out.append("{")
        for i, key in enumerate(sorted(value, key=lambda k: k.encode("utf-16-be"))):
            if i:
                out.append(",")
            out.append(_string(key))
            out.append(":")
            _write(value[key], out)
        out.append("}")
    else:
        raise NotCanonical(f"{type(value).__name__} has no JSON form")


def _string(text: str) -> str:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise NotCanonical("a string holds a lone surrogate") from None

    # With `ensure_ascii=False`, `json.dumps` escapes a string exactly as RFC 8785
    # asks: `\b \t \n \f \r` by name, other control characters as lower-case `\u00xx`,
    # `"` and `\`, and everything else as it is.
    #
    return json.dumps(text, ensure_ascii=False)


def sha256(text: str) -> str:
    """SHA-256 of the text's UTF-8 bytes, written `sha256:<hex>`."""
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def content_hash(item: dict[str, Any]) -> str:
    """
    Hash an item as the contract defines: everything but its state and bookkeeping.

    The item is the JSON the API returns for it, so a client can recompute this from
    what it received.
    """
    return sha256(
        canonical({k: v for k, v in item.items() if k not in CONTENT_HASH_EXCLUDES})
    )


def decision_hash(choice: dict[str, Any]) -> str:
    """
    Hash one decision: the item, its content hash, the action, and whatever the
    action needed. Fields that weren't chosen are left out, never sent as null.
    """
    return sha256(canonical({k: v for k, v in choice.items() if v is not None}))


def challenge(nonce: str, decision_hashes: list[str]) -> bytes:
    """The 32 bytes a passkey signs for a batch of decisions."""
    text = canonical({"nonce": nonce, "decisions": decision_hashes})
    return hashlib.sha256(text.encode()).digest()


def key_thumbprint(key: dict[str, str]) -> str:
    """
    An EC public key's RFC 7638 thumbprint, base64url: SHA-256 over the canonical JSON
    of its required members, which RFC 8785's ordering gives.
    """
    digest = hashlib.sha256(
        canonical({k: key[k] for k in ("crv", "kty", "x", "y")}).encode()
    ).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def unlock_challenge(nonce: str, thumbprint: str, idle_seconds: int) -> bytes:
    """
    The 32 bytes a passkey signs to unlock deciding: the tab's key, by its
    thumbprint, and how long it may be idle before it locks.
    """
    text = canonical(
        {"nonce": nonce, "unlock": thumbprint, "idle_seconds": idle_seconds}
    )
    return hashlib.sha256(text.encode()).digest()
