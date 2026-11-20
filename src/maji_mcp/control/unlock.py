"""
The keys a browser tab decides with while deciding is unlocked.

A passkey prompt on every decision is too much for a queue worked through in one
sitting, so one passkey assertion can unlock deciding in a tab (`api/openapi.yaml`,
"Deciding"). The tab makes an ECDSA P-256 key pair with WebCrypto that it can't
export, and keeps it only in memory. The passkey signs the key's thumbprint, and from
then on the tab signs each decision's challenge with that key, until it has been idle
too long.

WebCrypto's ECDSA signatures are the two 32-byte integers r and s side by side (IEEE
P1363), where `cryptography` expects DER, so they are re-encoded before verifying.
WebCrypto hashes what it signs, so the signature covers SHA-256 of the challenge's
32 bytes, and is verified the same way.
"""

# system imports
import base64
import binascii
from typing import Any

# 3rd party imports
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

# project imports
from maji_mcp.common.protocol import MajiError

COORDINATE = 32

# The members WebCrypto exports for a public key. A private key's `d` is refused.
#
JWK_MEMBERS = frozenset({"kty", "crv", "x", "y", "ext", "key_ops"})


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def check_key(key: Any) -> dict[str, str]:
    """
    A tab's public key as JWK: an EC P-256 point, and nothing else.

    Returns:
        The key's four members, ready to store and to hash for its thumbprint

    Raises:
        MajiError: `invalid_argument` for anything but a P-256 public key on the curve
    """
    if not isinstance(key, dict) or set(key) - JWK_MEMBERS:
        raise MajiError("invalid_argument", "key is an EC P-256 public key, as JWK")
    if key.get("kty") != "EC" or key.get("crv") != "P-256":
        raise MajiError("invalid_argument", "key is an EC P-256 public key, as JWK")
    try:
        x, y = _unb64(str(key["x"])), _unb64(str(key["y"]))
        if len(x) != COORDINATE or len(y) != COORDINATE:
            raise ValueError
        ec.EllipticCurvePublicNumbers(
            int.from_bytes(x), int.from_bytes(y), ec.SECP256R1()
        ).public_key()
    except KeyError, ValueError, binascii.Error:
        raise MajiError("invalid_argument", "key isn't a point on P-256") from None
    return {"kty": "EC", "crv": "P-256", "x": str(key["x"]), "y": str(key["y"])}


def verify(key: dict[str, str], data: bytes, signature: Any) -> bool:
    """Whether `signature`, base64url r||s, is the key's ECDSA P-256 signature of `data`."""
    if not isinstance(signature, str):
        return False
    try:
        raw = _unb64(signature)
    except ValueError, binascii.Error:
        return False
    if len(raw) != 2 * COORDINATE:
        return False
    public = ec.EllipticCurvePublicNumbers(
        int.from_bytes(_unb64(key["x"])),
        int.from_bytes(_unb64(key["y"])),
        ec.SECP256R1(),
    ).public_key()
    der = encode_dss_signature(
        int.from_bytes(raw[:COORDINATE]), int.from_bytes(raw[COORDINATE:])
    )
    try:
        public.verify(der, data, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True
