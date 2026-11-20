"""
The control plane's REST API, as `api/openapi.yaml` defines it, served by Starlette.

Every request passes three checks before a route sees it:
- **Host.** It must be the configured origin's host and port, against DNS rebinding.
- **Who is asking.** A bearer client token, or the session cookie set at sign-in.
  Only the hashes of either are stored.
- **Origin.** A request that isn't a GET, and isn't made with a bearer token, must
  carry an `Origin` equal to the control plane's own. A browser sends one on every
  such request, so a missing one is refused too. With the cookie's
  `SameSite=Strict`, this keeps another site from acting through your browser.

A token or a session is enough to read and to narrow access. Anything that widens
access needs a passkey assertion, which `passkeys.py` checks.

The store is called synchronously on the event loop (`store.py`).
"""

# system imports
import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# 3rd party imports
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

# project imports
from maji_mcp.common.protocol import MajiError
from maji_mcp.control.store import ControlStore

SESSION_COOKIE = "maji_session"
EVENT_POLL_SECONDS = 1.0
KEEPALIVE_SECONDS = 15.0
RESUME_WITHIN = timedelta(days=1)
PAGES_DIR = Path(__file__).parent / "pages"

# The pages the control plane serves itself load nothing but their own files, can't
# be framed, and send no referrer, so a code in a link's fragment stays on the page.
#
PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
        "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
PAGES = {
    "/setup": ("setup.html", "text/html; charset=utf-8"),
    "/setup.js": ("setup.js", "text/javascript; charset=utf-8"),
    "/setup.css": ("setup.css", "text/css; charset=utf-8"),
}
STATUS = {
    "not_found": 404,
    "invalid_argument": 422,
    "conflict": 409,
    "unauthenticated": 401,
    "permission_denied": 403,
    "stale": 410,
}
KINDS = (
    "changeset",
    "run_candidate",
    "promote",
    "grant",
    "connect",
    "pack_import",
    "pack_export",
    "policy_change",
    "question",
    "server_registration",
)


@dataclass(frozen=True)
class Settings:
    """
    Where the control plane is served, which is also what passkeys are bound to.

    Args:
        origin: the scheme, host and port browsers reach it at
        rp_id: the passkeys' relying party ID: the origin's host name
        rp_name: shown by the authenticator when a passkey is made
        web_dir: the built web client (`web/dist`), served at every path outside the
            API and the setup page; without it, only those are served
        unlock_idle_seconds: how long a tab unlocked for deciding may go unused
            before it locks
    """

    origin: str = "http://localhost:8767"
    rp_id: str = "localhost"
    rp_name: str = "maji-mcp"
    web_dir: Path | None = None
    unlock_idle_seconds: int = 10 * 60

    @property
    def host(self) -> str:
        return urlsplit(self.origin).netloc

    @property
    def secure(self) -> bool:
        return self.origin.startswith("https://")


def problem(status: int, code: str, title: str, data: Any = None) -> JSONResponse:
    """An RFC 9457 problem, with maji-mcp's error code."""
    body: dict[str, Any] = {"title": title, "status": status, "code": code}
    if data is not None:
        body["data"] = data
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def _from_error(exc: MajiError) -> JSONResponse:
    return problem(STATUS.get(exc.code, 500), exc.code, exc.message, exc.data or None)


###############################################################################
#
class Guard:
    """
    ASGI middleware: the Host check, who is asking, and the Origin rule.

    It puts `auth` in the request's state: `{"kind": "token", "client": ...}`,
    `{"kind": "session", "passkey": ..., "signed_in": ...}`, or None.
    """

    def __init__(self, app: Any, store: ControlStore, settings: Settings) -> None:
        self.app = app
        self.store = store
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        if headers.get("host") != self.settings.host:
            await problem(421, "permission_denied", "unknown host")(
                scope, receive, send
            )
            return

        auth: dict[str, Any] | None = None
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and token:
            client = self.store.client_for_token(token.strip())
            if client is None:
                await problem(401, "unauthenticated", "unknown token")(
                    scope, receive, send
                )
                return
            auth = {"kind": "token", "client": client}
        else:
            session_id = _cookie(headers.get("cookie", ""), SESSION_COOKIE)
            session = self.store.session(session_id) if session_id else None
            if session is not None:
                auth = {
                    "kind": "session",
                    "passkey": session["passkey"],
                    "signed_in": session["created"],
                    "session_id": session_id,
                }

        if scope["method"] not in ("GET", "HEAD") and (
            auth is None or auth["kind"] != "token"
        ):
            if headers.get("origin") != self.settings.origin:
                await problem(403, "permission_denied", "wrong or missing Origin")(
                    scope, receive, send
                )
                return
        scope.setdefault("state", {})["auth"] = auth
        await self.app(scope, receive, send)


def _cookie(header: str, name: str) -> str | None:
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name and value:
            return value
    return None


Handler = Callable[[Request], Awaitable[Response]]


def event_frames(store: ControlStore, after: int) -> tuple[list[str], int]:
    """
    The server-sent event frames for the events after a position, and the new
    position. An event carries only ids, never content.
    """
    events = store.events_after(after)
    frames = []
    for event in events:
        data = {k: v for k, v in event.items() if k not in ("seq", "at")}
        frames.append(
            f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(data)}\n\n"
        )
    return frames, events[-1]["seq"] if events else after


def resume_point(store: ControlStore, last_event_id: str | None) -> tuple[int, bool]:
    """
    Where a client's stream starts, and whether it must list the items again.

    A new client starts from now. One that reconnects resumes after the last event it
    saw, if that is no more than a day old; otherwise it is told to resync.
    """
    if last_event_id is None:
        return store.last_event(), False
    if not last_event_id.isdigit():
        raise MajiError("invalid_argument", "Last-Event-ID is a position in the stream")

    # Position 0 is the start of the stream, before any event: it is as old as the
    # first event, and a stream with no events yet is as new as now.
    #
    position = int(last_event_id)
    seen: str | None
    if position == 0:
        seen = store.event_time(1) or store.now()
    else:
        seen = store.event_time(position)
    cutoff = datetime.fromtimestamp(store.clock(), UTC) - RESUME_WITHIN
    cutoff_text = cutoff.isoformat(timespec="seconds")
    if seen is None or seen < cutoff_text:
        return store.last_event(), True
    return position, False


def signed_in(handler: Handler) -> Handler:
    """Refuse a request with neither a session nor a client token."""

    async def wrapped(request: Request) -> Response:
        if request.state.auth is None:
            return problem(401, "unauthenticated", "sign in, or send a client token")
        try:
            return await handler(request)
        except MajiError as exc:
            return _from_error(exc)

    return wrapped


def open_route(handler: Handler) -> Handler:
    """A route that needs no session or token, such as signing in."""

    async def wrapped(request: Request) -> Response:
        try:
            return await handler(request)
        except MajiError as exc:
            return _from_error(exc)

    return wrapped


def who(request: Request) -> str:
    """How a request is recorded in history: the client's name, or the passkey's id."""
    auth = request.state.auth
    if auth is None:
        return "nobody"
    return (
        f"client:{auth['client']}"
        if auth["kind"] == "token"
        else f"passkey:{auth['passkey']}"
    )


def _bool(value: str | None, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    if value in ("true", "1"):
        return True
    if value in ("false", "0"):
        return False
    raise MajiError("invalid_argument", f"expected true or false, not {value!r}")


def _limit(value: str | None) -> int:
    if value is None:
        return 50
    if not value.isdigit() or not 1 <= int(value) <= 200:
        raise MajiError("invalid_argument", "limit is 1 to 200")
    return int(value)


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        raise MajiError("invalid_argument", "the body isn't JSON") from None
    if not isinstance(body, dict):
        raise MajiError("invalid_argument", "the body must be a JSON object")
    return body


def client(web_dir: Path | None) -> list[Route | Mount]:
    """
    The web client's routes: its built files under /assets, and its page at every
    other path, since the client routes in the browser.

    The page goes out with the same policy as the setup page, so the client loads and
    reaches only its own origin. A path under /api never falls through to the page:
    an unknown API route stays a 404 problem.
    """
    if web_dir is None or not (web_dir / "index.html").exists():
        return []
    index = (web_dir / "index.html").read_bytes()

    async def page(request: Request) -> Response:
        if request.url.path.startswith("/api/"):
            return problem(404, "not_found", "no such route")
        return Response(
            index, media_type="text/html; charset=utf-8", headers=PAGE_HEADERS
        )

    return [
        Mount("/assets", StaticFiles(directory=web_dir / "assets")),
        Route("/{path:path}", page, methods=["GET"]),
    ]


###############################################################################
#
def build_app(
    store: ControlStore,
    settings: Settings | None = None,
    stopping: asyncio.Event | None = None,
) -> Guard:
    """
    Build the control plane's REST API.

    Args:
        store: the database
        settings: where it is served; the default is `http://localhost:8767`
        stopping: set when the server is stopping, which ends open event streams
    """
    settings = settings or Settings()
    stopping = stopping or asyncio.Event()

    @open_route
    async def health(request: Request) -> Response:
        return JSONResponse(
            {
                "ok": True,
                "instance": store.instance,
                "setup_needed": store.passkey_count() == 0,
            }
        )

    @signed_in
    async def list_items(request: Request) -> Response:
        q = request.query_params
        kind = q.get("kind")
        if kind is not None and kind not in KINDS:
            raise MajiError("invalid_argument", f"unknown kind {kind!r}")
        page = store.list_items(
            q.get("lane", ""),
            thread=q.get("thread"),
            kind=kind,
            blocking=_bool(q.get("blocking")),
            cursor=q.get("cursor"),
            limit=_limit(q.get("limit")),
        )
        return JSONResponse({"items": page.rows, "next_cursor": page.next_cursor})

    @signed_in
    async def next_item(request: Request) -> Response:
        page = store.list_items("ahead", limit=1)
        if not page.rows:
            return Response(status_code=204)
        return JSONResponse(page.rows[0])

    @signed_in
    async def get_item(request: Request) -> Response:
        item = store.get_item(request.path_params["item_id"])
        if item is None:
            raise MajiError("not_found", "no such item")
        return JSONResponse(item)

    @signed_in
    async def ask_again(request: Request) -> Response:
        # Lifting a refusal lets an agent put a path to you again, so it is yours
        # alone: an agent holding a client token mustn't lift its own refusal.
        #
        if request.state.auth["kind"] != "session":
            raise MajiError(
                "permission_denied", "only you, signed in, can let it ask again"
            )
        item = store.let_ask_again(request.path_params["item_id"], who(request))
        return JSONResponse(item)

    @signed_in
    async def list_decisions(request: Request) -> Response:
        q = request.query_params
        page = store.list_decisions(
            item_id=q.get("item_id"),
            by=q.get("by"),
            cursor=q.get("cursor"),
            limit=_limit(q.get("limit")),
        )
        return JSONResponse({"decisions": page.rows, "next_cursor": page.next_cursor})

    @signed_in
    async def get_decision(request: Request) -> Response:
        decision = store.get_decision(request.path_params["decision_id"])
        if decision is None:
            raise MajiError("not_found", "no such decision")
        return JSONResponse(decision)

    @signed_in
    async def get_policy(request: Request) -> Response:
        return JSONResponse({"rules": store.policy_rules()})

    def _grant_filters(request: Request) -> dict[str, Any]:
        q = request.query_params
        return {
            "principal": q.get("principal"),
            "active": _bool(q.get("active"), True),
            "resource": q.get("resource"),
            "access": q.get("access"),
            "lasts": q.get("lasts"),
            "session": q.get("session"),
        }

    @signed_in
    async def list_grants(request: Request) -> Response:
        q = request.query_params
        page = store.list_grants(
            q=q.get("q"),
            cursor=q.get("cursor"),
            limit=_limit(q.get("limit")),
            **_grant_filters(request),
        )
        return JSONResponse({"grants": page.rows, "next_cursor": page.next_cursor})

    @signed_in
    async def group_grants(request: Request) -> Response:
        q = request.query_params
        groups = store.group_grants(
            q.get("by", ""), active=bool(_bool(q.get("active"), True)), q=q.get("q")
        )
        return JSONResponse({"groups": groups})

    @signed_in
    async def revoke_grants(request: Request) -> Response:
        body = await _json(request)
        ids = body.get("grant_ids")
        if ids is not None:
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                raise MajiError("invalid_argument", "grant_ids is a list of ids")
        elif isinstance(body.get("filter"), dict):
            allowed = {"principal", "resource", "access", "lasts", "session"}
            given = body["filter"]
            if not given or set(given) - allowed:
                raise MajiError("invalid_argument", f"filter by {sorted(allowed)}")
            ids = store.matching_grant_ids(**given)
        else:
            raise MajiError("invalid_argument", "give grant_ids or a filter")
        return JSONResponse({"revoked": store.revoke_grants(ids, who(request))})

    @signed_in
    async def revoke_grant(request: Request) -> Response:
        grant_id = request.path_params["grant_id"]
        if store.get_grant(grant_id) is None:
            raise MajiError("not_found", "no such grant")
        store.revoke_grants([grant_id], who(request))
        return Response(status_code=204)

    @signed_in
    async def list_connections(request: Request) -> Response:
        return JSONResponse({"connections": store.list_connections()})

    @signed_in
    async def request_connection(request: Request) -> Response:
        auth = request.state.auth
        principal = "you" if auth["kind"] == "session" else f"client:{auth['client']}"
        item, created = store.request_connection(
            request.path_params["service"], {"principal": principal}
        )
        return JSONResponse(item, status_code=201 if created else 200)

    @signed_in
    async def events(request: Request) -> Response:
        after, resync = resume_point(store, request.headers.get("last-event-id"))

        async def stream() -> AsyncIterator[str]:
            nonlocal after
            if resync:
                yield f'id: {after}\nevent: resync\ndata: {{"type": "resync"}}\n\n'
            quiet = 0.0
            while not stopping.is_set() and not await request.is_disconnected():
                frames, after = event_frames(store, after)
                for frame in frames:
                    yield frame
                quiet = 0.0 if frames else quiet + EVENT_POLL_SECONDS
                if quiet >= KEEPALIVE_SECONDS:
                    yield ": keep-alive\n\n"
                    quiet = 0.0
                await asyncio.sleep(EVENT_POLL_SECONDS)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    from maji_mcp.control import passkeys

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/items", list_items, methods=["GET"]),
        Route("/items/next", next_item, methods=["GET"]),
        Route("/items/{item_id}", get_item, methods=["GET"]),
        Route("/items/{item_id}/ask-again", ask_again, methods=["POST"]),
        Route("/decisions", list_decisions, methods=["GET"]),
        Route("/decisions/{decision_id}", get_decision, methods=["GET"]),
        Route("/policy", get_policy, methods=["GET"]),
        Route("/grants", list_grants, methods=["GET"]),
        Route("/grants/groups", group_grants, methods=["GET"]),
        Route("/grants/revocations", revoke_grants, methods=["POST"]),
        Route("/grants/{grant_id}", revoke_grant, methods=["DELETE"]),
        Route("/connections", list_connections, methods=["GET"]),
        Route("/connections/{service}/requests", request_connection, methods=["POST"]),
        Route("/events", events, methods=["GET"]),
        *passkeys.routes(store, settings),
    ]

    async def crashed(request: Request, exc: Exception) -> Response:
        # Anything unexpected is answered as a problem like any other, saying nothing
        # about what went wrong inside.
        #
        return problem(500, "internal", "the control plane failed; see its log")

    def page(name: str, media: str) -> Handler:
        body = (PAGES_DIR / name).read_bytes()

        async def serve(request: Request) -> Response:
            return Response(body, media_type=media, headers=PAGE_HEADERS)

        return serve

    pages = [Route(path, page(*file), methods=["GET"]) for path, file in PAGES.items()]

    async def missing(request: Request, exc: Exception) -> Response:
        return problem(404, "not_found", "no such route")

    app = Starlette(
        routes=[Mount("/api/v0", routes=routes), *pages, *client(settings.web_dir)],
        exception_handlers={404: missing, Exception: crashed},
    )
    return Guard(app, store, settings)
