"""
Helpers for testing the control plane against its contract and with a passkey.

- `fits` validates a response against the response schema `api/openapi.yaml` gives
  for its route, method and status.
- `Authenticator` is a software passkey: a P-256 key that registers and signs the way
  a platform authenticator does, so the server's real verification runs in tests.
"""

# system imports
import base64
import hashlib
import json
import secrets
from typing import Any

# 3rd party imports
import cbor2
import httpx
import yaml
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

# project imports
from tests.support.paths import SPEC_FILE

SPEC: dict[str, Any] = yaml.safe_load(SPEC_FILE.read_text())
BASE = "urn:maji-api"
REGISTRY = Registry().with_resource(
    BASE, Resource.from_contents(SPEC, default_specification=DRAFT202012)
)
PREFIX = "/api/v0"


def _pointer(*parts: str) -> str:
    """The JSON pointer to a place in the contract, with each part escaped."""
    return "/" + "/".join(p.replace("~", "~0").replace("/", "~1") for p in parts)


def fits(response: httpx.Response, route: str, method: str) -> None:
    """
    Assert that a response is one the contract allows for the route.

    Args:
        response: the response
        route: the route as the contract writes it, such as `/items/{item_id}`
        method: the HTTP method, in lower case
    """
    operation = SPEC["paths"][route][method]
    status = str(response.status_code)
    assert status in operation["responses"], (route, method, status, response.text)
    where = ["paths", route, method, "responses", status]
    declared = operation["responses"][status]
    if "$ref" in declared:
        name = declared["$ref"].rsplit("/", 1)[1]
        where = ["components", "responses", name]
        declared = SPEC["components"]["responses"][name]
    content = declared.get("content")
    if not content:
        assert not response.content, (route, status, response.text)
        return
    media = response.headers["content-type"].split(";")[0].strip()
    assert media in content, (route, status, media)
    validator = Draft202012Validator(
        {"$ref": BASE + "#" + _pointer(*where, "content", media, "schema")},
        registry=REGISTRY,
    )
    errors = sorted(validator.iter_errors(response.json()), key=lambda e: e.path)
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors[:5]]


def b64(data: bytes) -> str:
    """Bytes as base64url without padding, as WebAuthn writes them."""
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(text: str) -> bytes:
    """The bytes of base64url text, padded or not."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Authenticator:
    """
    A software passkey for one relying party.

    Args:
        rp_id: the relying party ID it registers under
        counter: whether it keeps a signature counter, as hardware keys do; platform
            passkeys report zero
    """

    def __init__(self, rp_id: str = "localhost", counter: bool = False) -> None:
        self.rp_id = rp_id
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(16)
        self.count = 0
        self.counter = counter

    @property
    def id(self) -> str:
        """The credential's id, as the control plane names the passkey."""
        return b64(self.credential_id)

    def _auth_data(self, flags: int, attested: bytes = b"") -> bytes:
        """The authenticator data for one answer, counting it if the key counts."""
        if self.counter:
            self.count += 1
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        return rp_hash + bytes([flags]) + self.count.to_bytes(4, "big") + attested

    def register(self, options: dict[str, Any], origin: str) -> dict[str, Any]:
        """Answer `navigator.credentials.create()` with these options."""
        numbers = self.key.public_key().public_numbers()
        cose = cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )
        attested = bytes(16) + len(self.credential_id).to_bytes(2, "big")
        attested += self.credential_id + cose
        client_data = json.dumps(
            {
                "type": "webauthn.create",
                "challenge": options["challenge"],
                "origin": origin,
            }
        ).encode()
        attestation = cbor2.dumps(
            {"fmt": "none", "attStmt": {}, "authData": self._auth_data(0x45, attested)}
        )
        return {
            "id": self.id,
            "rawId": self.id,
            "type": "public-key",
            "response": {
                "attestationObject": b64(attestation),
                "clientDataJSON": b64(client_data),
            },
            "clientExtensionResults": {},
        }

    def sign(
        self,
        challenge: str,
        origin: str,
        verified: bool = True,
        repeat_count: bool = False,
    ) -> dict[str, Any]:
        """
        Answer `navigator.credentials.get()` for a challenge, as a browser at `origin`.

        Args:
            challenge: the challenge, base64url
            origin: the page asking, which the browser writes into the client data
            verified: whether the user was verified, by Touch ID or a PIN
            repeat_count: send the last counter value again, as a cloned key would
        """
        if repeat_count:
            self.count -= 1
        flags = 0x01 | (0x04 if verified else 0)
        auth_data = self._auth_data(flags)
        client_data = json.dumps(
            {"type": "webauthn.get", "challenge": challenge, "origin": origin}
        ).encode()
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": self.id,
            "rawId": self.id,
            "type": "public-key",
            "response": {
                "authenticatorData": b64(auth_data),
                "clientDataJSON": b64(client_data),
                "signature": b64(signature),
                "userHandle": None,
            },
            "clientExtensionResults": {},
        }
