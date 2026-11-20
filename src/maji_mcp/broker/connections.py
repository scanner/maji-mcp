"""
Connections: the services the broker calls with a key you paste, and how the key gets
from your browser to the broker's credential store.

Each such service has a definition (`Service`): the only host its key is sent to, how
a request carries the key, what the key must be allowed, where you make one, and what
is given with it, such as an organization slug. The broker reports the definitions to
the control plane, with the connection it holds for each: once the control plane
answers, after each change, and every minute, so the web app can list them and the
control plane can build a Connect item from the broker's own words.

**When a stub needs a key the broker doesn't hold,** `key_for` asks the control plane
for the service's Connect item and fails at once, naming the item. The agent goes on
with other work and calls again once you've connected the service. A script that loops
asks once: the item is remembered for `ASK_EVERY` seconds, and the control plane keeps
one Connect item waiting for a service at most. Once you decline, the broker stops
asking until it restarts; you can still connect the service from the web app.

**When you connect a service,** the decision feed brings your decision. `apply` takes
the key from the control plane, once (`control/handoff.py`), checks it is the one whose
fingerprint you signed, and makes one read at the service with it. Only a key the
service takes is stored. A later Connect decision replaces the key, and Disconnect, a
reconsideration of the decision in effect, deletes it.

What an agent or the control plane hears about a key is its fingerprint, never the
key: the outcome recorded for a decision is written here, never taken from an error
that might quote a request.
"""

# system imports
import asyncio
import hashlib
import hmac
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# project imports
from maji_mcp.broker.catalog import CallContext
from maji_mcp.broker.credentials import Credential, CredentialStore
from maji_mcp.common.protocol import MajiError, call_unix

# How long a Connect item the broker asked for is remembered before asking the control
# plane about it again, so a script that loops over a stub makes one request.
#
ASK_EVERY = 30.0

# How often the broker reports its services to the control plane: soon again while
# the control plane can't be reached, as when the two start together, and then now
# and again, so a control plane restarted or restored from a backup learns them too.
#
REPORT_RETRY = 2.0
REPORT_EVERY = 60.0

# What a stub is given to call its service: the key, and what was given with it.
#
KeyFor = Callable[[CallContext], Awaitable[tuple[str, dict[str, str]]]]

# Checks a key at the service before it is stored, with the settings given with it.
# Returns None when the service takes the key, or why it didn't, in words of the
# broker's own: never the service's answer, which could quote the request.
#
Check = Callable[[str, dict[str, str]], Awaitable[str | None]]


@dataclass(frozen=True)
class Setting:
    """Something given with a key, none of it secret, such as an organization slug."""

    name: str
    label: str
    pattern: str
    description: str = ""


@dataclass(frozen=True)
class Service:
    """
    A service the broker can call with a key: `Service` in the contract, and the check
    a key has to pass.

    Attributes:
        name: the service's name, which is also its stubs' namespace
        label: its name for people
        host: the only host its key is sent to, from the broker's configuration
        sent_as: how a request carries the key, such as `Authorization: Bearer`
        permissions: what the key must be allowed, in the service's own terms
        key_page: where you make a key
        check: makes one read with a key, before it is stored
        settings: what is given with the key
        url: the address the stubs call, on `host`
    """

    name: str
    label: str
    host: str
    sent_as: str
    permissions: tuple[str, ...]
    key_page: str
    check: Check = field(repr=False)
    settings: tuple[Setting, ...] = ()
    url: str = ""

    def definition(self) -> dict[str, Any]:
        """The service as the contract describes it."""
        return {
            "service": self.name,
            "label": self.label,
            "method": "api_key",
            "host": self.host,
            "sent_as": self.sent_as,
            "permissions": list(self.permissions),
            "key_page": self.key_page,
            "settings": [
                {
                    "name": s.name,
                    "label": s.label,
                    "pattern": s.pattern,
                    **({"description": s.description} if s.description else {}),
                }
                for s in self.settings
            ],
        }


def fingerprint(key: str) -> str:
    """A key's fingerprint, as a Connect decision names it: its SHA-256."""
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()


class Connections:
    """
    The services the broker can call with a key, and the keys it holds for them.

    Args:
        services: the services' definitions
        store: the credential store; without one, no service can be connected
        control_plane: the control plane's socket for the broker; without one,
            nothing can be asked for or reported
        clock: seconds, for how long an item asked for is remembered
    """

    def __init__(
        self,
        services: Sequence[Service],
        store: CredentialStore | None,
        control_plane: Path | None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.services = {s.name: s for s in services}
        self.store = store
        self.control_plane = control_plane
        self.clock = clock
        self._asked: dict[str, tuple[str, float]] = {}
        self._declined: dict[str, str] = {}

    def key_for(self, service: str) -> KeyFor:
        """
        What a service's stubs call for its key: the key and its settings, or an
        error that names the Connect item waiting for you.
        """

        async def key(ctx: CallContext) -> tuple[str, dict[str, str]]:
            held = self.store.get(service) if self.store else None
            if held is not None:
                return held.key, held.settings
            raise await self._not_connected(service, ctx)

        return key

    async def _not_connected(self, service: str, ctx: CallContext) -> MajiError:
        """The error for a stub whose service has no key, having asked you for one."""
        label = self.services[service].label
        if self.store is None or self.control_plane is None:
            return MajiError(
                "unavailable",
                f"{label} can't be connected: this broker has no "
                "credential store or control plane",
            )
        if service not in self._declined:
            item = await self._waiting_item(service, ctx)
            if item is not None:
                return MajiError(
                    "unavailable",
                    f"{label} isn't connected. The user is asked to connect it in "
                    f"their queue ({item}); call again once they have, and carry on "
                    "meanwhile.",
                    {"item": item},
                )
        return MajiError(
            "permission_denied",
            f"the user declined to connect {label} ({self._declined[service]}); "
            "ask them before relying on it",
        )

    async def _waiting_item(self, service: str, ctx: CallContext) -> str | None:
        """
        The Connect item waiting for a service, asked for if there is none, or None
        once you've declined it.
        """
        assert self.control_plane is not None
        now = self.clock()
        asked = self._asked.get(service)
        if asked is not None and now - asked[1] < ASK_EVERY:
            return asked[0]
        if asked is not None:
            status = await call_unix(
                self.control_plane, "status_of", {"item": asked[0]}
            )
            decision = status.get("decision") or {}
            if status["state"] == "decided" and decision.get("action") == "deny":
                self._declined[service] = asked[0]
                del self._asked[service]
                return None
        requester: dict[str, Any] = {"principal": ctx.principal, "run": ctx.run_id}
        if ctx.session:
            requester["session"] = ctx.session
        asking = {
            "service": service,
            "requester": requester,
            "claim": f"A script called a {service} stub, which needs a key.",
        }
        try:
            made = await call_unix(self.control_plane, "request_connection", asking)
        except MajiError as exc:
            # A control plane that hasn't heard of the service hasn't had the broker's
            # report yet, as when it started after the broker. Report, and ask again.
            #
            if exc.code != "not_found":
                raise
            await self.report()
            made = await call_unix(self.control_plane, "request_connection", asking)
        item = str(made["item"]["id"])
        self._asked[service] = (item, now)
        return item

    async def report(self) -> None:
        """Tell the control plane each service, and the connection held for it."""
        if self.control_plane is None:
            return
        services = []
        for name, service in sorted(self.services.items()):
            held = self.store.get(name) if self.store else None
            services.append(
                {
                    **service.definition(),
                    "connection": held.described() if held else None,
                }
            )
        await call_unix(self.control_plane, "report_services", {"services": services})

    async def run(self) -> None:
        """
        Report the services for good: retrying until the control plane answers, then
        every `REPORT_EVERY` seconds.
        """
        while True:
            try:
                await self.report()
                await asyncio.sleep(REPORT_EVERY)
            except OSError, MajiError:
                await asyncio.sleep(REPORT_RETRY)

    async def _reported(self) -> None:
        """Report after a change, leaving a report that fails to `run`'s next pass."""
        try:
            await self.report()
        except (OSError, MajiError) as exc:
            print(f"broker: services not reported: {type(exc).__name__}", flush=True)

    async def apply(
        self, decision: dict[str, Any], item: dict[str, Any]
    ) -> tuple[str, str] | None:
        """
        Apply a decision on a Connect item: store the key it brought, or delete the
        key for Disconnect. Returns the outcome to record, or None for a decision
        that isn't on a Connect item, or changes nothing, such as Deny.
        """
        if item.get("kind") != "connect":
            return None
        name = str((item.get("connection") or {}).get("service", ""))
        service = self.services.get(name)
        if service is None or self.store is None:
            return ("failed", f"this broker can't connect {name or 'that service'}")
        action = decision.get("action")
        if action == "disconnect":
            return await self._disconnect(service, decision)
        if action != "connect":
            return None
        held = self.store.get(name)
        if held is not None and held.decision == decision["id"]:
            return ("applied", f"connected {service.label}")
        return await self._connect(service, decision, item)

    async def _connect(
        self, service: Service, decision: dict[str, Any], item: dict[str, Any]
    ) -> tuple[str, str]:
        assert self.control_plane is not None and self.store is not None
        again = f"connect {service.label} again from the web app"
        try:
            taken = await call_unix(
                self.control_plane, "take_credential", {"decision": decision["id"]}
            )
        except MajiError:
            return ("failed", f"the key didn't reach the broker: {again}")
        key = taken.get("key")
        signed = str(decision.get("credential_fingerprint", ""))
        if not isinstance(key, str) or not hmac.compare_digest(
            fingerprint(key), signed
        ):
            return ("failed", f"the key isn't the one you signed for: {again}")
        settings = decision.get("credential_settings") or {}
        wanted = {s.name: s for s in service.settings}
        if set(settings) != set(wanted) or not all(
            isinstance(v, str) and re.fullmatch(wanted[k].pattern, v)
            for k, v in settings.items()
        ):
            return ("failed", f"the settings don't fit {service.label}: {again}")
        try:
            refused = await service.check(key, dict(settings))
        except Exception as exc:  # any failure is reported in words of our own
            refused = f"{service.label} couldn't be reached ({type(exc).__name__})"
        if refused is not None:
            return ("failed", f"{refused}: {again}")
        self.store.put(
            service.name,
            Credential(
                key=key,
                settings=dict(settings),
                fingerprint=signed,
                connected_at=datetime.now(UTC).isoformat(timespec="seconds"),
                decision=str(decision["id"]),
                item=str(item["id"]),
            ),
        )
        self._declined.pop(service.name, None)
        self._asked.pop(service.name, None)
        await self._reported()
        return ("applied", f"connected {service.label}, key {signed[:15]}")

    async def _disconnect(
        self, service: Service, decision: dict[str, Any]
    ) -> tuple[str, str]:
        assert self.store is not None
        held = self.store.get(service.name)
        if held is not None and held.decision != decision.get("supersedes"):
            return ("conflict", f"a later key replaced this one for {service.label}")
        self.store.delete(service.name)
        await self._reported()
        return ("applied", f"disconnected {service.label}")
