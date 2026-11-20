"""
Tests for the control plane's REST API: the checks every request passes, and the
routes `api.py` serves itself: the queue's items, letting a requester ask again,
grants, the event stream, and the pages.

Each response is checked against the schema `api/openapi.yaml` gives for it. The
routes that need a passkey are in `test_passkeys.py` and `test_unlock.py`.
"""

# system imports
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.common.names import session_name
from maji_mcp.control import canonical
from maji_mcp.control.api import build_app
from maji_mcp.control.store import ControlStore
from tests.control.factories import REPOSITORY, MountFactory
from tests.support.contract import PREFIX, fits
from tests.support.control import (
    INSTANCE,
    ORIGIN,
    Control,
    Touches,
    choice,
    serving,
)
from tests.support.services import FakeClock

# Routes the contract defines that aren't served yet, each waiting on something
# outside the control plane: disconnecting by service and OAuth sign-in on the
# broker's OAuth client, resources on its run log, and policy proposals on the form
# policy rules take.
#
PENDING = {
    ("/resources", "get"),
    ("/connections/{service}", "delete"),
    ("/oauth/callback", "get"),
    ("/policy/proposals", "post"),
}


###############################################################################
# The contract as a whole


def test_every_route_in_the_contract_is_served_or_listed_as_pending(
    store: ControlStore, spec: dict[str, Any]
) -> None:
    """
    GIVEN: The routes the contract defines
     WHEN: they are compared with the routes the control plane serves
     THEN: each is served, or is on the short list waiting on another service, and
           nothing is served that the contract doesn't define
    """
    mounted = build_app(store).app.routes[0]
    served = {
        (route.path, method.lower())
        for route in mounted.routes
        for method in route.methods or []
        if method != "HEAD"
    }
    defined = {
        (path, method)
        for path, item in spec["paths"].items()
        for method in item
        if method in ("get", "post", "put", "delete", "patch")
    }
    assert served | PENDING == defined
    assert not served & PENDING


###############################################################################
# Who may ask


async def test_a_request_for_another_host_is_refused(control: Control) -> None:
    """
    GIVEN: A request whose Host isn't the control plane's configured one, as a DNS
           rebinding attack would send
     WHEN: it arrives
     THEN: it is refused before any route sees it
    """
    response = await control.get("/health", headers={"Host": "127.0.0.1:8767"})
    assert response.status_code == 421
    fits(response, "/health", "get")


@pytest.mark.parametrize(
    "route", ["/items", "/events"], ids=["the queue", "the event stream"]
)
async def test_reading_needs_a_session_or_a_token(control: Control, route: str) -> None:
    """
    GIVEN: A request with neither a session nor a client token
     WHEN: it reads the queue, or asks for the event stream
     THEN: it is refused with 401
    """
    refused = await control.get(route)
    assert refused.status_code == 401
    fits(refused, route, "get")


async def test_a_session_change_needs_the_control_planes_origin(
    control: Control, passkey: Touches, granted: Callable[..., dict[str, Any]]
) -> None:
    """
    GIVEN: A signed-in browser session
     WHEN: a request that changes something arrives with the session's cookie and no
           Origin, or another site's Origin
     THEN: it is refused, while one with the control plane's own Origin, or one made
           with a bearer token and no Origin, goes through
    """
    grant = granted(access="read")
    url = f"{PREFIX}/grants/{grant['id']}"

    missing = await control.http.delete(url)
    assert missing.status_code == 403
    fits(missing, "/grants/{grant_id}", "delete")
    foreign = await control.http.delete(
        url, headers={"Origin": "http://localhost:9999"}
    )
    assert foreign.status_code == 403
    fits(foreign, "/grants/{grant_id}", "delete")

    own = await control.http.delete(url, headers={"Origin": ORIGIN})
    assert own.status_code == 204
    revocations = await control.http.post(
        f"{PREFIX}/grants/revocations",
        json={"grant_ids": [grant["id"]]},
        headers=control.bearer(),
    )
    assert revocations.status_code == 200
    fits(revocations, "/grants/revocations", "post")


###############################################################################
# The queue


async def test_the_queue_shows_an_item_as_its_content_hash_was_computed(
    control: Control, queued: Callable[..., dict[str, Any]], faker: Faker
) -> None:
    """
    GIVEN: A changeset in the queue
     WHEN: a client with a token lists the queue, asks for the next item, and
           fetches it
     THEN: each read succeeds and fits the contract, and the item's content hash
           recomputes from the JSON the client received, without the fields the
           contract leaves out, so a client can check what it is about to sign
      AND: the item names the requester's session for people, worked out from the
           session the hash covers
    """
    item = queued(words=faker.sentence())

    listed = await control.get(
        "/items", params={"lane": "ahead"}, headers=control.bearer()
    )
    assert listed.status_code == 200
    fits(listed, "/items", "get")
    assert [i["id"] for i in listed.json()["items"]] == [item["id"]]
    following = await control.get("/items/next", headers=control.bearer())
    fits(following, "/items/next", "get")
    fetched = await control.get(f"/items/{item['id']}", headers=control.bearer())
    fits(fetched, "/items/{item_id}", "get")

    received = fetched.json()
    assert canonical.content_hash(received) == received["content_hash"]
    assert received["session_name"] == session_name(item["requester"]["session"])


async def test_an_empty_queue_and_an_unknown_item(control: Control) -> None:
    """
    GIVEN: Nothing waiting
     WHEN: a client asks for the next item, and for an item that doesn't exist
     THEN: it gets 204 for the first and 404 for the second
    """
    empty = await control.get("/items/next", headers=control.bearer())
    assert empty.status_code == 204
    missing = await control.get(
        f"/items/{INSTANCE}-01jc6z8q3v7m2k4t9r5x1y0b8n", headers=control.bearer()
    )
    assert missing.status_code == 404
    fits(missing, "/items/{item_id}", "get")


###############################################################################
# Letting a requester ask again


@pytest.fixture
def decided_mount(
    queued: Callable[..., dict[str, Any]], recorded: Callable[..., dict[str, Any]]
) -> Callable[..., dict[str, Any]]:
    """
    A function that queues a request from an agent's session to mount a repository,
    decides it, and returns the item: refused, unless told to approve it.
    """

    def decided_mount(action: str = "deny") -> dict[str, Any]:
        item = queued(MountFactory, requester__in_session=True)

        # A mount is approved only with its pattern remembered.
        #
        remember = [{"pattern": "p1", "lasts": "session"}]
        recorded(item, action, remember=remember if action == "approve" else None)
        return item

    return decided_mount


async def test_you_can_let_an_agent_ask_again_for_a_mount_you_refused(
    control: Control,
    passkey: Touches,
    store: ControlStore,
    clock: FakeClock,
    decided_mount: Callable[..., dict[str, Any]],
) -> None:
    """
    GIVEN: A mount you refused, which the broker refuses the agent's reruns for
     WHEN: you, signed in, let it ask again from the item, and then do so again
     THEN: the item says when and by whom, and the broker's status for it says the
           same, so the agent's next run asks you again
      AND: doing it again changes nothing and raises no second event, and the record
           can't be changed or removed afterwards
    """
    item = decided_mount()
    path = f"/items/{item['id']}/ask-again"

    lifted = await control.post(path, {})
    assert lifted.status_code == 200, lifted.text
    fits(lifted, "/items/{item_id}/ask-again", "post")
    asked = lifted.json()["ask_again"]
    assert asked["by"].startswith("passkey:")
    assert store.status_of(item["id"])["ask_again"] == asked["at"]

    clock.now += 60
    again = await control.post(path, {})
    assert again.json()["ask_again"] == asked
    updated = [e for e in store.events_after(0) if e["type"] == "item.updated"]
    assert len(updated) == 1
    with pytest.raises(Exception, match="append-only"):
        store.db.execute("DELETE FROM asking_again")


@pytest.mark.parametrize(
    ("make", "status"),
    [
        (lambda queued, decided: queued(MountFactory, requester__in_session=True), 422),
        (lambda queued, decided: decided("approve"), 422),
        (lambda queued, decided: queued(), 422),
        (lambda queued, decided: {"id": f"{INSTANCE}-nothing"}, 404),
    ],
    ids=["still waiting", "approved", "not a mount", "unknown"],
)
async def test_only_a_refused_mount_can_be_asked_for_again(
    control: Control,
    passkey: Touches,
    store: ControlStore,
    queued: Callable[..., dict[str, Any]],
    decided_mount: Callable[..., dict[str, Any]],
    make: Callable[..., dict[str, Any]],
    status: int,
) -> None:
    """
    GIVEN: A mount still waiting, a mount you approved, a change that isn't a mount,
           or no item at all
     WHEN: you try to let its requester ask again
     THEN: it is refused, as nothing was refused that could be lifted, and nothing is
           recorded
    """
    item = make(queued, decided_mount)
    answer = await control.post(f"/items/{item['id']}/ask-again", {})
    assert answer.status_code == status, answer.text
    fits(answer, "/items/{item_id}/ask-again", "post")
    assert store.db.execute("SELECT * FROM asking_again").fetchall() == []


async def test_an_agent_with_a_token_cant_lift_its_own_refusal(
    control: Control,
    store: ControlStore,
    decided_mount: Callable[..., dict[str, Any]],
) -> None:
    """
    GIVEN: A mount you refused, and a client holding only a token, as an agent could
     WHEN: the client asks to let the requester ask again
     THEN: it is refused with 403, and the broker still refuses the agent's reruns
    """
    item = decided_mount()
    answer = await control.post(
        f"/items/{item['id']}/ask-again", {}, headers=control.bearer()
    )
    assert answer.status_code == 403
    assert answer.json()["title"] == "only you, signed in, can let it ask again"
    fits(answer, "/items/{item_id}/ask-again", "post")
    assert "ask_again" not in store.status_of(item["id"])


###############################################################################
# Grants


async def test_a_grant_with_a_time_lapses_then(
    control: Control,
    passkey: Touches,
    clock: FakeClock,
    queued: Callable[..., dict[str, Any]],
) -> None:
    """
    GIVEN: An approval remembered until a time an hour away, written with an offset
     WHEN: the grants are listed before and after that time
     THEN: the grant is active before, and gone from the active list after
    """
    item = queued(MountFactory)

    # 10:00 at +01:00 is 09:00 UTC, an hour after the clock.
    #
    until = "2027-01-15T10:00:00+01:00"
    decided = await control.decide(
        passkey,
        choice(item, remember=[{"pattern": "p1", "lasts": "until", "until": until}]),
    )
    assert decided.status_code == 201

    active = await control.get("/grants", headers=control.bearer())
    assert len(active.json()["grants"]) == 1
    clock.now += 2 * 60 * 60
    lapsed = await control.get("/grants", headers=control.bearer())
    assert lapsed.json()["grants"] == []


async def test_grants_are_found_by_fuzzy_search_and_counted_in_groups(
    control: Control, granted: Callable[..., dict[str, Any]]
) -> None:
    """
    GIVEN: Grants covering two repositories and a service, for two principals
     WHEN: a client searches for a fragment of one repository's name, then counts
           grants by resource
     THEN: the search finds the grants for that repository, best match first, each
           saying where it matched; the counts put the larger group first
    """
    granted(access="read")
    granted()
    granted(access="read", resource="~/src/web", principal="advisor")
    granted(access="read", resource="sentry")

    # A fragment of the repository's path finds the read and the mount of it, and
    # neither of the other grants.
    #
    found = await control.get(
        "/grants", params={"q": "srcapi"}, headers=control.bearer()
    )
    fits(found, "/grants", "get")
    grants = found.json()["grants"]
    assert {g["pattern"]["resource"] for g in grants} == {REPOSITORY}
    assert all(g["match"]["ranges"] for g in grants)

    # Counted by resource, the repository with two grants comes first.
    #
    groups = await control.get(
        "/grants/groups", params={"by": "resource"}, headers=control.bearer()
    )
    fits(groups, "/grants/groups", "get")
    assert groups.json()["groups"][0] == {
        "key": REPOSITORY,
        "count": 2,
        "permanent": 2,
        "last_used_at": None,
        "next_expiry": None,
    }


async def test_a_group_of_grants_is_revoked_at_once(
    control: Control, granted: Callable[..., dict[str, Any]]
) -> None:
    """
    GIVEN: Three active grants, two of them for one repository
     WHEN: a client revokes every grant for that repository
     THEN: two are revoked, they no longer list as active, and the third remains
    """
    for resource in (REPOSITORY, REPOSITORY, "sentry"):
        granted(access="read", resource=resource)

    revoked = await control.post(
        "/grants/revocations",
        {"filter": {"resource": REPOSITORY}},
        headers=control.bearer(),
    )
    fits(revoked, "/grants/revocations", "post")
    assert revoked.json() == {"revoked": 2}
    active = await control.get("/grants", headers=control.bearer())
    assert [g["pattern"]["resource"] for g in active.json()["grants"]] == ["sentry"]


async def test_the_grants_of_one_session_are_found_and_revoked_together(
    control: Control, granted: Callable[..., dict[str, Any]], faker: Faker
) -> None:
    """
    GIVEN: Two grants held for one session, one for another session, and one made
           in the first session that lasts until revoked
     WHEN: a client counts grants by session, lists the first session's, and
           revokes them
     THEN: each session is a group, named for people, and the grant that outlives
           its session groups under (none) without a name; the first session's
           grants list together, and revoking them leaves the others, the one that
           lasts until revoked included
    """
    ended, current = faker.sha256(), faker.sha256()
    granted("session", session=ended)
    granted("session", resource="~/src/web", session=ended)
    granted("session", session=current)
    granted("permanent", resource="~/notes", session=ended)

    # Each session is a group with its name; the grant kept until revoked is held
    # by no session, though it was made in one.
    #
    counted = await control.get(
        "/grants/groups", params={"by": "session"}, headers=control.bearer()
    )
    fits(counted, "/grants/groups", "get")
    assert [
        (g["key"], g["session_name"], g["count"]) for g in counted.json()["groups"]
    ] == [
        (ended, session_name(ended), 2),
        ("(none)", None, 1),
        (current, session_name(current), 1),
    ]

    # Listing by the first session gives the two grants it holds.
    #
    listed = await control.get(
        "/grants", params={"session": ended}, headers=control.bearer()
    )
    fits(listed, "/grants", "get")
    assert sorted(g["pattern"]["resource"] for g in listed.json()["grants"]) == [
        REPOSITORY,
        "~/src/web",
    ]

    # Revoking by that session takes those two, and leaves the other session's
    # grant and the one kept until revoked.
    #
    revoked = await control.post(
        "/grants/revocations", {"filter": {"session": ended}}, headers=control.bearer()
    )
    fits(revoked, "/grants/revocations", "post")
    assert revoked.json() == {"revoked": 2}
    left = await control.get("/grants", headers=control.bearer())
    assert {
        (g["session"], g["lasts"], g["pattern"]["resource"])
        for g in left.json()["grants"]
    } == {
        (current, "session", REPOSITORY),
        (ended, "permanent", "~/notes"),
    }


###############################################################################
# Live updates


@pytest.fixture
def stream(control: Control) -> Callable[..., Awaitable[str]]:
    """
    A function that follows the event stream from a position with a client's token,
    as a browser that disconnects after a moment, and returns what was sent.
    """

    async def stream(last_event_id: int, wait: float) -> str:
        headers = {**control.bearer(), "Last-Event-ID": str(last_event_id)}
        sent: list[bytes] = []
        first = True

        async def receive() -> dict[str, Any]:
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(wait)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body":
                sent.append(message.get("body", b""))

        # The stream ends only when its client goes away, which an HTTP client that
        # waits for the whole response never does, so the application is called
        # directly.
        #
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        scope = {
            "type": "http",
            "method": "GET",
            "path": f"{PREFIX}/events",
            "raw_path": f"{PREFIX}/events".encode(),
            "query_string": b"",
            "headers": [(b"host", b"localhost:8767"), *raw],
            "scheme": "http",
            "server": ("localhost", 8767),
            "client": ("127.0.0.1", 50000),
            "root_path": "",
            "http_version": "1.1",
        }
        await control.app(scope, receive, send)  # type: ignore[arg-type]
        return b"".join(sent).decode()

    return stream


async def test_a_reconnecting_client_gets_the_events_it_missed(
    store: ControlStore,
    queued: Callable[..., dict[str, Any]],
    recorded: Callable[..., dict[str, Any]],
    stream: Callable[..., Awaitable[str]],
) -> None:
    """
    GIVEN: A client that saw events up to some position, after which an item was
           created and decided
     WHEN: it reconnects with that position as Last-Event-ID
     THEN: it gets the events after it, in order, each with only ids
    """
    seen = store.last_event()
    item = queued()
    recorded(item)

    text = await stream(seen, 0.2)

    assert "event: item.created" in text and "event: item.decided" in text
    assert text.index("item.created") < text.index("item.decided")
    assert item["title"] not in text


async def test_a_client_that_was_away_a_day_is_told_to_resync(
    store: ControlStore,
    clock: FakeClock,
    queued: Callable[..., dict[str, Any]],
    stream: Callable[..., Awaitable[str]],
) -> None:
    """
    GIVEN: A client whose last event is more than a day old
     WHEN: it reconnects with it
     THEN: the stream starts with resync, telling it to list the items again
    """
    queued()
    old = store.last_event()
    clock.now += 2 * 24 * 60 * 60

    text = await stream(old, 0.1)

    assert text.startswith(f"id: {old}\nevent: resync")


###############################################################################
# The pages


async def test_the_setup_page_loads_only_its_own_files(control: Control) -> None:
    """
    GIVEN: The setup page, which reads a one-time code from its link's fragment
     WHEN: a browser loads it and its script
     THEN: both come with a policy that lets the page load and reach only its own
           origin, never be framed, and send no referrer; and the script is served
           as JavaScript, which the page's policy will run
    """
    page = await control.http.get("/setup")
    assert page.status_code == 200
    policy = page.headers["content-security-policy"]
    script = await control.http.get("/setup.js")

    # Each header is a rule of its own, so each is reported on its own.
    #
    with check:
        assert "default-src 'none'" in policy and "script-src 'self'" in policy
    with check:
        assert "frame-ancestors 'none'" in policy
    with check:
        assert page.headers["referrer-policy"] == "no-referrer"
    with check:
        assert script.headers["content-type"].startswith("text/javascript")


@pytest.fixture
async def web_client(store: ControlStore, tmp_path: Path) -> AsyncIterator[Control]:
    """A browser at a control plane that serves a built web client beside its API."""
    web = tmp_path / "dist"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>maji-mcp</title>")
    (web / "assets" / "index.js").write_text("export {};")
    async with serving(store, web_dir=web) as browser:
        yield browser


async def test_the_web_client_is_served_at_its_own_paths(web_client: Control) -> None:
    """
    GIVEN: A control plane with the built web client
     WHEN: a browser opens one of the client's pages, and loads its script
     THEN: the page comes with a policy that lets it load and reach only its own
           origin, fonts included, and its script is served as a file
    """
    page = await web_client.http.get("/queue")
    assert page.status_code == 200
    assert page.text.startswith("<!doctype html>")
    policy = page.headers["content-security-policy"]
    script = await web_client.http.get("/assets/index.js")

    with check:
        assert "script-src 'self'" in policy and "font-src 'self'" in policy
    with check:
        assert "connect-src 'self'" in policy and "frame-ancestors 'none'" in policy
    with check:
        assert script.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/v0/nothing-here",
        "/api/v1/items",
        "/assets/%2e%2e/%2e%2e/etc/passwd",
    ],
    ids=[
        "an unknown API route",
        "another API version",
        "a path climbing out of assets",
    ],
)
async def test_the_web_client_serves_only_its_page_and_its_own_files(
    web_client: Control, path: str
) -> None:
    """
    GIVEN: A control plane serving the web client's page at every path outside the
           API, and the client's files from its assets directory
     WHEN: a request names an API route that doesn't exist, or climbs out of the
           assets directory with `..`
     THEN: it gets a 404 problem: never the client's page in place of an API route,
           and nothing from outside the directory
    """
    response = await web_client.http.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/")
