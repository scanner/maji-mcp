"""
What tests drive the control plane with: a client of its REST API, a tab's key pair,
a passkey that counts its prompts, and its two internal sockets with their callers.

The software passkey itself, and the check of a response against the contract, are
in `tests/support/contract.py`.
"""

# system imports
import asyncio
import contextlib
import shutil
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 3rd party imports
import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

# project imports
from maji_mcp.common.protocol import MajiError, call_unix
from maji_mcp.control import internal
from maji_mcp.control.api import Guard, Settings, build_app
from maji_mcp.control.store import ControlStore
from tests.support.contract import PREFIX, Authenticator, b64, fits, unb64

ORIGIN = "http://localhost:8767"
INSTANCE = "studio-mac"


def choice(
    item: dict[str, Any], action: str = "approve", **extra: Any
) -> dict[str, Any]:
    """
    A decision on an item as a client sends it: the item, the content hash the client
    saw, the action, and whatever else was chosen.

    Args:
        item: the item decided on
        action: one of the actions the item offers
        extra: the choice's other fields, such as `parts`, `remember` or `note`; ones
            given as None are left out, as a client leaves out what wasn't chosen
    """
    chosen = {k: v for k, v in extra.items() if v is not None}
    return {
        "item": item["id"],
        "content_hash": item["content_hash"],
        "action": action,
        **chosen,
    }


class Control:
    """
    A client of the control plane's REST API, at the control plane's own origin.

    It keeps the session cookie a sign-in sets, as a browser does, and can send a
    client's token instead, as the CLI does.

    Args:
        http: the HTTP client, which keeps the session's cookie
        app: the application it calls, for a test that drives it without HTTP
        token: a client token the control plane knows
        setup_code: an unspent setup code, which registers the first passkey
    """

    def __init__(
        self, http: httpx.AsyncClient, app: Guard, token: str, setup_code: str
    ) -> None:
        self.http = http
        self.app = app
        self.token = token
        self.setup_code = setup_code

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        """Read a route of the API, as the browser does."""
        return await self.http.get(PREFIX + path, **kwargs)

    async def post(self, path: str, body: Any, **kwargs: Any) -> httpx.Response:
        """Post JSON to a route of the API, from the control plane's own origin."""
        headers = {"Origin": ORIGIN, **kwargs.pop("headers", {})}
        return await self.http.post(PREFIX + path, json=body, headers=headers, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        """Delete at a route of the API, from the control plane's own origin."""
        headers = {"Origin": ORIGIN, **kwargs.pop("headers", {})}
        return await self.http.delete(PREFIX + path, headers=headers, **kwargs)

    def bearer(self) -> dict[str, str]:
        """The header that sends the client's token, in place of a session."""
        return {"Authorization": f"Bearer {self.token}"}

    async def register(
        self, key: Authenticator, name: str = "laptop", **proof: str
    ) -> httpx.Response:
        """Register a passkey, on the proof given: a setup code or a recovery code."""
        options = await self.post("/passkeys/registration/options", {"name": name})
        fits(options, "/passkeys/registration/options", "post")
        body = options.json()
        credential = key.register(body["public_key"], ORIGIN)
        return await self.post(
            "/passkeys",
            {
                "registration_id": body["registration_id"],
                "credential": credential,
                **proof,
            },
        )

    async def sign_in(self, key: Authenticator) -> httpx.Response:
        """Sign in with a passkey, which sets the session's cookie on this client."""
        options = (await self.post("/session/options", {})).json()
        assertion = key.sign(options["public_key"]["challenge"], ORIGIN)
        return await self.post(
            "/session",
            {"challenge_id": options["challenge_id"], "assertion": assertion},
        )

    async def challenge(self, *choices: Any) -> httpx.Response:
        """Ask, with the client's token, for the challenge a decision signs."""
        return await self.post(
            "/decisions/challenge", {"decisions": list(choices)}, headers=self.bearer()
        )

    async def send(self, challenge_id: str, **proof: Any) -> httpx.Response:
        """Send, with the client's token, the decision a challenge was issued for."""
        return await self.post(
            "/decisions",
            {"challenge_id": challenge_id, **proof},
            headers=self.bearer(),
        )

    async def decide(
        self,
        key: Authenticator,
        *choices: dict[str, Any],
        origin: str = ORIGIN,
        secrets: dict[str, str] | None = None,
        **kw: Any,
    ) -> httpx.Response:
        """
        Decide with a passkey: one challenge for `choices`, signed by `key`.

        Args:
            key: the passkey that signs
            choices: the decisions, one for each item
            origin: the page the browser says is asking
            secrets: keys pasted into Connect items, by item id, sent beside the
                signed choices as the web client sends them
            kw: how the passkey signs, as `Authenticator.sign` takes it

        Returns:
            The answer to the decision, or to the challenge if none was issued
        """
        issued = await self.challenge(*choices)
        fits(issued, "/decisions/challenge", "post")
        if issued.status_code != 200:
            return issued
        body = issued.json()
        assertion = key.sign(body["public_key"]["challenge"], origin, **kw)
        extra = {"secrets": secrets} if secrets is not None else {}
        return await self.send(body["challenge_id"], assertion=assertion, **extra)

    async def unlock(self, passkey: Authenticator, tab: TabKey) -> httpx.Response:
        """Unlock deciding in a tab: its key, certified by one passkey assertion."""
        options = await self.post("/unlocks/options", {"key": tab.jwk})
        fits(options, "/unlocks/options", "post")
        if options.status_code != 200:
            return options
        body = options.json()
        assertion = passkey.sign(body["public_key"]["challenge"], ORIGIN)
        made = await self.post(
            "/unlocks", {"challenge_id": body["challenge_id"], "assertion": assertion}
        )
        fits(made, "/unlocks", "post")
        return made

    async def decide_unlocked(
        self, tab: TabKey, unlock_id: str, *choices: dict[str, Any]
    ) -> httpx.Response:
        """Decide from an unlocked tab: the challenge signed by its key alone."""
        issued = await self.post("/decisions/challenge", {"decisions": list(choices)})
        fits(issued, "/decisions/challenge", "post")
        body = issued.json()
        signature = tab.sign(unb64(body["public_key"]["challenge"]))
        decided = await self.post(
            "/decisions",
            {
                "challenge_id": body["challenge_id"],
                "unlock": {"id": unlock_id, "signature": signature},
            },
        )
        fits(decided, "/decisions", "post")
        return decided


@contextlib.asynccontextmanager
async def serving(
    store: ControlStore,
    *,
    token: str = "",
    setup_code: str = "",
    web_dir: Path | None = None,
) -> AsyncIterator[Control]:
    """
    Serve the REST API over a database, and yield a client of it with no session yet.

    Each call is a browser of its own, so two of them over one database are two
    sessions of one control plane.

    Args:
        store: the database
        token: a client token already added to the database
        setup_code: a setup code already added to the database
        web_dir: the built web client to serve beside the API
    """
    app = build_app(store, Settings(origin=ORIGIN, web_dir=web_dir))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as http:
        yield Control(http, app, token, setup_code)


class TabKey:
    """A tab's key pair, as WebCrypto makes it: P-256, signing r||s over SHA-256."""

    def __init__(self) -> None:
        self.private = ec.generate_private_key(ec.SECP256R1())
        numbers = self.private.public_key().public_numbers()
        self.jwk = {
            "kty": "EC",
            "crv": "P-256",
            "x": b64(numbers.x.to_bytes(32)),
            "y": b64(numbers.y.to_bytes(32)),
            "ext": True,
            "key_ops": ["verify"],
        }

    def sign(self, data: bytes) -> str:
        """Sign `data` as WebCrypto does: r and s side by side, base64url."""
        r, s = decode_dss_signature(self.private.sign(data, ec.ECDSA(hashes.SHA256())))
        return b64(r.to_bytes(32) + s.to_bytes(32))


class Touches(Authenticator):
    """A passkey that counts its prompts: every assertion is one touch."""

    def __init__(self) -> None:
        super().__init__()
        self.touches = 0

    def sign(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Answer a challenge as the passkey does, and count the touch."""
        self.touches += 1
        return super().sign(*args, **kwargs)


class Caller:
    """One of the control plane's internal sockets, as the service that calls it."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def call(self, method: str, **params: Any) -> Any:
        """Call one of the socket's methods, and return its result."""
        return await call_unix(self.path, method, params)

    async def refused(self, method: str, **params: Any) -> str:
        """Make a call that has to be refused, and return the refusal's code."""
        with pytest.raises(MajiError) as raised:
            await self.call(method, **params)
        return raised.value.code


class Sockets:
    """The control plane's two internal sockets, served from the directory `root`."""

    def __init__(self, root: Path) -> None:
        self.broker = Caller(root / "broker" / internal.SOCKET)
        self.gateway = Caller(root / "gateway" / internal.SOCKET)


@dataclass(frozen=True)
class Plane:
    """
    A control plane without its REST API: what the broker and the gateway reach.

    Attributes:
        store: its database, where a test reads the queue and records your decisions
        broker_socket: the socket the broker raises items, asks for grants and reads
            decisions on
        gateway_socket: the socket the gateway asks on
    """

    store: ControlStore
    broker_socket: Path
    gateway_socket: Path


@contextlib.asynccontextmanager
async def serving_sockets(store: ControlStore) -> AsyncIterator[Path]:
    """
    Serve the control plane's two internal sockets over `store` for as long as the
    block runs.

    Yields:
        The directory the sockets are served from, which `Sockets` and `Plane` take:
        the broker's is in `broker/` and the gateway's in `gateway/`.
    """
    # macOS limits a Unix socket's path to about 104 bytes, and pytest's tmp_path is
    # longer than that, so the sockets live in a short directory of their own.
    #
    root = Path(tempfile.mkdtemp(prefix="maji-", dir="/tmp"))
    (root / "broker").mkdir()
    (root / "gateway").mkdir()
    servers = await internal.serve(store, root / "broker", root / "gateway")
    try:
        yield root
    finally:
        for server in servers:
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in servers))
        shutil.rmtree(root)
