"""
The control plane's internal sockets: how the broker and the gateway reach the queue.

JSON-RPC over Unix sockets, like the broker's own (`common/protocol.py`). There is one
socket for each caller, in a volume only that caller mounts, because they need
different things and are trusted with different things. Neither can decide anything:
decisions enter only through the REST API, with a passkey.

**The broker's socket** carries what the broker computes and what it acts on:
- `create_item`: a changeset, grant or connect request, or a question an MCP server
  asked, with facts and patterns the broker computed
- `decisions_since`: the decisions after a position, each with its item, for the
  broker to apply in order
- `record_outcome`: what applying a decision did
- `grant_covers`: whether an active grant covers a request
- `status_of`: what became of an item it raised, such as which parts were refused
- `withdraw`: take back an item it raised that a later request replaces

**The gateway's socket** carries what an agent may ask for itself:
- `submit_question`: a question for you. The question is the item's title, and why
  it matters is its claim, both labelled as the agent's words; it can set no facts
  and offer no pattern, so nothing it writes can widen access.
- `item_status`: what became of one of its requests
- `withdraw`: take back one of its requests while it waits

The gateway passes the principal it authenticated; a principal sees and withdraws
only its own items, and can't read anyone's decisions in bulk.
"""

# system imports
from collections.abc import Callable
from pathlib import Path
from typing import Any

# project imports
from maji_mcp.common.protocol import MajiError, UnknownMethod, serve_unix
from maji_mcp.control.store import ACTIONS, CONTENT_FIELDS, ControlStore

ACCESS = ("read", "mount", "structural", "write")
SOCKET = "control.sock"
MAX_QUESTION = 300
MAX_WHY = 2000
MAX_CHOICES = 10
MAX_CHOICE = 200


def _text(
    params: dict[str, Any], name: str, *, optional: bool = False, limit: int = 4000
) -> str | None:
    value = params.get(name)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MajiError(
            "invalid_argument", f"{name} is a non-empty string of at most {limit}"
        )
    return value


def _flag(params: dict[str, Any], name: str) -> bool:
    value = params.get(name, False)
    if not isinstance(value, bool):
        raise MajiError("invalid_argument", f"{name} is true or false")
    return value


def _requester(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("principal"), str):
        raise MajiError("invalid_argument", "requester needs a principal")
    extra = set(value) - {"principal", "session", "run", "machine", "client"}
    if extra:
        raise MajiError("invalid_argument", f"unknown requester fields {sorted(extra)}")
    for field in ("session", "run", "machine"):
        if value.get(field) is not None and not isinstance(value[field], str):
            raise MajiError("invalid_argument", f"requester.{field} is a string")
    client = value.get("client")
    if client is not None and (
        not isinstance(client, dict)
        or not isinstance(client.get("name"), str)
        or not set(client) <= {"name", "version"}
        or not all(isinstance(v, str) and len(v) <= 100 for v in client.values())
    ):
        raise MajiError(
            "invalid_argument", "requester.client is a name and perhaps a version"
        )
    return value


def _facts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(
        isinstance(f, dict)
        and isinstance(f.get("label"), str)
        and isinstance(f.get("value"), str)
        for f in value
    ):
        raise MajiError("invalid_argument", "facts is a list of labels and values")
    return value


class Socket:
    """
    One internal socket's methods: the public methods of a subclass, and no others.

    Args:
        store: the control plane's database
    """

    def __init__(self, store: ControlStore) -> None:
        self.store = store

    async def handle(self, method: str, params: dict[str, Any]) -> Any:
        if method.startswith("_") or method in ("handle", "store"):
            raise UnknownMethod(method)
        handler: Callable[[dict[str, Any]], Any] | None = getattr(self, method, None)
        if handler is None or not callable(handler):
            raise UnknownMethod(method)
        return handler(params)


class BrokerSocket(Socket):
    """The broker's socket: items it computed, and the decisions it applies."""

    def create_item(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = _text(params, "kind")
        if kind not in ACTIONS:
            raise MajiError("invalid_argument", f"unknown kind {kind!r}")
        unknown = (
            set(params) - set(CONTENT_FIELDS) - {"priority", "blocking", "expires_at"}
        )
        if unknown or {"id", "created_at"} & set(params):
            raise MajiError(
                "invalid_argument",
                f"unknown fields {sorted(unknown | ({'id', 'created_at'} & set(params)))}",
            )
        content = {
            k: v
            for k, v in params.items()
            if k in CONTENT_FIELDS and k not in ("kind", "title", "requester", "facts")
        }
        priority = params.get("priority", 0)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise MajiError("invalid_argument", "priority is an integer")
        return self.store.create_item(
            str(kind),
            str(_text(params, "title")),
            _requester(params.get("requester")),
            _facts(params.get("facts")),
            priority=priority,
            blocking=_flag(params, "blocking"),
            expires_at=_text(params, "expires_at", optional=True),
            **content,
        )

    def decisions_since(self, params: dict[str, Any]) -> dict[str, Any]:
        position = params.get("position", 0)
        limit = params.get("limit", 100)
        if not isinstance(position, int) or isinstance(position, bool) or position < 0:
            raise MajiError("invalid_argument", "position is a non-negative integer")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 500
        ):
            raise MajiError("invalid_argument", "limit is 1 to 500")
        return self.store.decisions_since(position, limit)

    def record_outcome(self, params: dict[str, Any]) -> dict[str, Any]:
        self.store.record_outcome(
            str(_text(params, "decision")),
            str(_text(params, "outcome")),
            _text(params, "detail", optional=True) or "",
        )
        return {}

    def grant_covers(self, params: dict[str, Any]) -> dict[str, Any]:
        access = _text(params, "access")
        if access not in ACCESS:
            raise MajiError("invalid_argument", f"access is one of {list(ACCESS)}")
        grant = self.store.grant_covers(
            str(_text(params, "principal")),
            str(access),
            str(_text(params, "resource")),
            session=_text(params, "session", optional=True),
            run=_text(params, "run", optional=True),
            exact=params.get("exact") is True,
        )
        return {"grant": grant}

    def status_of(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.store.status_of(str(_text(params, "item")))

    def report_services(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Record the services the broker can call with a key, and the connection it
        holds for each, in place of what it reported before.
        """
        services = params.get("services")
        if not isinstance(services, list):
            raise MajiError("invalid_argument", "services is a list")
        self.store.report_services(services)
        return {}

    def request_connection(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Ask you to connect a service a script needed, with the Connect item built
        from the service's definition, or the one already waiting for it.
        """
        item, created = self.store.request_connection(
            str(_text(params, "service")),
            _requester(params.get("requester")),
            _text(params, "claim", optional=True),
        )
        return {"item": item, "created": created}

    def take_credential(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Hand over the key pasted with a Connect decision, once: the control plane
        forgets it as it answers (`handoff.py`).
        """
        key = self.store.keys.take(str(_text(params, "decision")))
        if key is None:
            raise MajiError(
                "not_found",
                "no key is waiting for that decision: it was taken, or lost",
            )
        return {"key": key}

    def withdraw(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Take back an item the broker raised for a principal, while it waits: one a
        later request replaces, such as a skill saved again.
        """
        state = self.store.withdraw(
            str(_text(params, "item")), str(_text(params, "principal"))
        )
        return {"state": state}


class GatewaySocket(Socket):
    """The gateway's socket: an agent's own questions, and its own requests' state."""

    def submit_question(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Put an agent's question to you.

        The question is what you're asked, so it is the item's title, with its
        whitespace run together, and the list shows it. It and the choices are the
        agent's words, and why it matters is the agent's claim; clients label them
        as the requester's. Who asked, from which session, and whether they are
        waiting on the answer are the control plane's own record, in the item's
        requester and `blocking`; it has no facts of its own.

        A question is a sentence or two, up to `MAX_QUESTION` characters; the
        context for it goes in `why`.
        """
        allowed = {
            "principal",
            "session",
            "run",
            "question",
            "why",
            "choices",
            "blocking",
            "expires_at",
        }
        unknown = set(params) - allowed
        if unknown:
            raise MajiError("invalid_argument", f"unknown fields {sorted(unknown)}")
        principal = str(_text(params, "principal", limit=200))
        session = _text(params, "session", optional=True, limit=200)
        run = _text(params, "run", optional=True, limit=200)
        question = str(_text(params, "question", limit=MAX_QUESTION))
        why = _text(params, "why", optional=True, limit=MAX_WHY)
        choices = params.get("choices") or []
        if (
            not isinstance(choices, list)
            or len(choices) > MAX_CHOICES
            or not all(
                isinstance(c, str) and c.strip() and len(c) <= MAX_CHOICE
                for c in choices
            )
        ):
            raise MajiError(
                "invalid_argument",
                f"choices is up to {MAX_CHOICES} strings of at most {MAX_CHOICE}",
            )
        blocking = _flag(params, "blocking")
        requester = {"principal": principal, "session": session, "run": run}
        return self.store.create_item(
            "question",
            " ".join(question.split()),
            {k: v for k, v in requester.items() if v},
            [],
            blocking=blocking,
            expires_at=_text(params, "expires_at", optional=True),
            claim=why,
            choices=[{"id": f"c{i + 1}", "label": c} for i, c in enumerate(choices)]
            or None,
        )

    def item_status(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.store.item_status(
            str(_text(params, "item")), str(_text(params, "principal"))
        )

    def withdraw(self, params: dict[str, Any]) -> dict[str, Any]:
        state = self.store.withdraw(
            str(_text(params, "item")), str(_text(params, "principal"))
        )
        return {"state": state}


async def serve(store: ControlStore, broker_dir: Path, gateway_dir: Path) -> list[Any]:
    """
    Serve each caller's socket in its own directory, which only that caller's
    container mounts.
    """
    return [
        await serve_unix(broker_dir / SOCKET, BrokerSocket(store).handle, mode=0o660),
        await serve_unix(gateway_dir / SOCKET, GatewaySocket(store).handle, mode=0o660),
    ]
