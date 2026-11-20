"""
Tests for connecting the broker to a service with a key you paste
(`src/maji_mcp/broker/connections.py`).

The control plane is real and in-process: its database and its socket for the broker.
Your decisions are recorded in its database as the REST API records them once your
passkey's proof holds, and the key is held for the broker as the REST API holds it.
The service is a made-up tracker, whose check of a key a test decides.
"""

# system imports
import hashlib
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.broker.catalog import CallContext
from maji_mcp.broker.connections import ASK_EVERY, Connections, Service, Setting
from maji_mcp.broker.credentials import CredentialStore
from maji_mcp.broker.decisions import DecisionFeed
from maji_mcp.common.protocol import MajiError
from tests.support.control import Plane
from tests.support.services import FakeClock


class Tracker:
    """A made-up service, and what it does with the keys it is shown."""

    def __init__(self) -> None:
        self.takes = True
        self.shown: list[tuple[str, dict[str, str]]] = []

    async def check(self, key: str, settings: dict[str, str]) -> str | None:
        """Take or refuse a key, as the test says, recording what it was shown."""
        self.shown.append((key, settings))
        return None if self.takes else "the tracker refused the key"

    def service(self) -> Service:
        return Service(
            name="tracker",
            label="Tracker",
            host="tracker.example",
            sent_as="Authorization: Bearer",
            permissions=("issues:read",),
            key_page="https://tracker.example/keys",
            check=self.check,
            settings=(Setting("org", "Organization slug", "[a-z]{1,20}"),),
        )


class Broker:
    """The broker's side of connections, and the feed that brings your decisions."""

    def __init__(self, connections: Connections, feed: DecisionFeed) -> None:
        self.connections = connections
        self.feed = feed

    async def key(self) -> tuple[str, dict[str, str]]:
        """Ask for the tracker's key, as one of its stubs does in a script's call."""
        return await self.connections.key_for("tracker")(
            CallContext("reader", "run-1", "ab" * 32)
        )

    async def refusal(self) -> MajiError:
        """The error a stub gets for the tracker's key, when there isn't one."""
        with pytest.raises(MajiError) as raised:
            await self.key()
        return raised.value


@pytest.fixture
def tracker() -> Tracker:
    """The made-up service, taking every key it is shown until told otherwise."""
    return Tracker()


@pytest.fixture
async def broker(
    plane: Plane, tracker: Tracker, tmp_path: Path, clock: FakeClock
) -> Broker:
    """
    The broker's connections to the tracker, with a credential store holding no key,
    reported to the control plane as the broker reports them when it starts.
    """
    connections = Connections(
        [tracker.service()],
        CredentialStore(tmp_path / "credentials"),
        plane.broker_socket,
        clock,
    )
    await connections.report()
    feed = DecisionFeed(plane.broker_socket, tmp_path / "position", [connections.apply])
    return Broker(connections, feed)


@pytest.fixture
def key(faker: Faker) -> str:
    """A made-up key."""
    return "tok_" + faker.password(length=40, special_chars=False)


def decide(
    plane: Plane,
    item: dict[str, Any],
    action: str,
    key: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """
    Record your decision on an item, as the REST API does once your passkey's proof
    holds, holding the pasted key for the broker if there is one.
    """
    choice: dict[str, Any] = {
        "item": item["id"],
        "content_hash": item["content_hash"],
        "action": action,
        **extra,
    }
    if action == "connect":
        signed = key or "a key that never reached the broker"
        choice["credential_fingerprint"] = (
            "sha256:" + hashlib.sha256(signed.encode()).hexdigest()
        )
        choice.setdefault("credential_settings", {"org": "acme"})
    [decision] = plane.store.record_decisions([choice], {"kind": "you", "passkey": "k"})
    if key is not None:
        plane.store.keys.hold(decision["id"], key)
    return decision


async def test_a_script_needing_a_key_asks_you_once_and_carries_on(
    broker: Broker, plane: Plane, clock: FakeClock
) -> None:
    """
    GIVEN: A service the broker holds no key for
     WHEN: a script calls one of its stubs over and over, and again after a while
     THEN: each call fails at once as unavailable, naming the Connect item in your
           queue, so the agent can carry on with other work
      AND: there is one Connect item, asked for by the script's principal and run,
           however often it calls
    """
    errors = [await broker.refusal() for _ in range(5)]
    clock.now += ASK_EVERY + 1
    errors.append(await broker.refusal())

    [item] = plane.store.list_items("ahead").rows
    with check:
        assert {e.code for e in errors} == {"unavailable"}
    with check:
        assert {e.data["item"] for e in errors} == {item["id"]}
    with check:
        assert "Tracker isn't connected" in errors[0].message
    with check:
        assert item["requester"] == {
            "principal": "reader",
            "run": "run-1",
            "session": "ab" * 32,
        }


async def test_a_key_you_connect_is_checked_kept_and_used(
    broker: Broker, plane: Plane, tracker: Tracker, key: str
) -> None:
    """
    GIVEN: A Connect item for a service
     WHEN: you connect it with a key, and the broker reads the decision
     THEN: the key is checked once at the service, with the settings given with it,
           and kept, so stubs get it from then on
      AND: the control plane is told the service is connected, by the key's
           fingerprint, and the decision's outcome says so without the key
    """
    item, _ = plane.store.request_connection("tracker", {"principal": "you"})
    decision = decide(plane, item, "connect", key)

    await broker.feed.catch_up()

    applied = plane.store.get_decision(decision["id"])["applied"]  # type: ignore[index]
    [connection] = plane.store.list_connections()
    with check:
        assert tracker.shown == [(key, {"org": "acme"})]
    with check:
        assert await broker.key() == (key, {"org": "acme"})
    with check:
        assert applied["outcome"] == "applied"
    with check:
        assert key not in applied["detail"]
    with check:
        assert (connection["status"], connection["fingerprint"]) == (
            "connected",
            decision["credential_fingerprint"],
        )
    with check:
        assert key not in "\n".join(plane.store.db.iterdump())


async def test_a_service_given_nothing_beside_its_key_connects(
    plane: Plane, tmp_path: Path, clock: FakeClock, key: str
) -> None:
    """
    GIVEN: A service that takes a key and nothing beside it, as GitHub does
     WHEN: you connect it, with no settings, and the broker reads the decision
     THEN: the control plane records the decision, and the broker checks the key and
           keeps it, so stubs get it from then on
    """
    shown: list[tuple[str, dict[str, str]]] = []

    async def check_key(pasted: str, settings: dict[str, str]) -> str | None:
        shown.append((pasted, settings))
        return None

    service = Service(
        name="board",
        label="Board",
        host="board.example",
        sent_as="Authorization: Bearer",
        permissions=("read",),
        key_page="https://board.example/keys",
        check=check_key,
    )
    connections = Connections(
        [service], CredentialStore(tmp_path / "credentials"), plane.broker_socket, clock
    )
    await connections.report()
    feed = DecisionFeed(plane.broker_socket, tmp_path / "position", [connections.apply])
    item, _ = plane.store.request_connection("board", {"principal": "you"})
    decision = decide(plane, item, "connect", key, credential_settings={})

    await feed.catch_up()

    applied = plane.store.get_decision(decision["id"])["applied"]  # type: ignore[index]
    held = await connections.key_for("board")(CallContext("reader", "run-1"))
    with check:
        assert (applied["outcome"], shown, held) == ("applied", [(key, {})], (key, {}))


@pytest.mark.parametrize(
    ("held", "takes", "said"),
    [
        pytest.param(False, True, "the key didn't reach the broker", id="key lost"),
        pytest.param(True, False, "the tracker refused the key", id="refused"),
    ],
)
async def test_a_key_that_cant_be_kept_leaves_the_service_unconnected(
    broker: Broker,
    plane: Plane,
    tracker: Tracker,
    key: str,
    held: bool,
    takes: bool,
    said: str,
) -> None:
    """
    GIVEN: A Connect item for a service
     WHEN: you connect it, but the key never reaches the broker, as when the control
           plane restarts first, or the service refuses the key
     THEN: nothing is kept, the outcome says why and to connect the service again
           from the web app, and stubs go on asking you to connect it
    """
    tracker.takes = takes
    item, _ = plane.store.request_connection("tracker", {"principal": "you"})
    decision = decide(plane, item, "connect", key if held else None)

    await broker.feed.catch_up()

    applied = plane.store.get_decision(decision["id"])["applied"]  # type: ignore[index]
    with check:
        assert applied["outcome"] == "failed"
    with check:
        assert applied["detail"].startswith(said)
    with check:
        assert "connect Tracker again from the web app" in applied["detail"]
    with check:
        assert (await broker.refusal()).code == "unavailable"


async def test_disconnecting_deletes_the_key_but_never_a_later_one(
    broker: Broker, plane: Plane, key: str, faker: Faker
) -> None:
    """
    GIVEN: A service connected by one Connect decision
     WHEN: you connect it again with a new key, and disconnect from the first item
           before the broker has read either decision; then disconnect from the
           second item
     THEN: the first Disconnect deletes nothing, since the new key replaced the one
           it was for, and its outcome says so
      AND: the second deletes the key: stubs ask you to connect the service again,
           and the control plane is told it isn't connected
    """
    first, _ = plane.store.request_connection("tracker", {"principal": "you"})
    connected_first = decide(plane, first, "connect", key)
    await broker.feed.catch_up()

    # Both decisions are made before the broker reads either, so the control plane
    # still offers Disconnect on the first item.
    #
    second, _ = plane.store.request_connection("tracker", {"principal": "you"})
    connected_second = decide(plane, second, "connect", faker.sha256())
    late = decide(
        plane,
        plane.store.get_item(first["id"]),  # type: ignore[arg-type]
        "disconnect",
        supersedes=connected_first["id"],
    )
    await broker.feed.catch_up()
    late_outcome = plane.store.get_decision(late["id"])["applied"]  # type: ignore[index]
    still = await broker.key()

    disconnected = decide(
        plane,
        plane.store.get_item(second["id"]),  # type: ignore[arg-type]
        "disconnect",
        supersedes=connected_second["id"],
    )
    await broker.feed.catch_up()

    with check:
        assert late_outcome["outcome"] == "conflict"
    with check:
        assert late_outcome["detail"] == "a later key replaced this one for Tracker"
    with check:
        assert still[0] != key
    with check:
        assert plane.store.get_decision(disconnected["id"])["applied"]["outcome"] == (  # type: ignore[index]
            "applied"
        )
    with check:
        assert (await broker.refusal()).code == "unavailable"
    with check:
        assert plane.store.list_connections()[0]["status"] == "not_connected"


async def test_once_you_decline_the_broker_stops_asking(
    broker: Broker, plane: Plane, clock: FakeClock
) -> None:
    """
    GIVEN: A Connect item a script's call asked for
     WHEN: you deny it, and the script calls the service's stubs again later
     THEN: each call is refused as permission_denied, saying you declined, and no
           new Connect item is put to you
    """
    asked = await broker.refusal()
    item = plane.store.get_item(asked.data["item"])
    decide(plane, item, "deny")  # type: ignore[arg-type]

    clock.now += ASK_EVERY + 1
    later = [await broker.refusal() for _ in range(3)]

    with check:
        assert {e.code for e in later} == {"permission_denied"}
    with check:
        assert "declined to connect Tracker" in later[0].message
    with check:
        assert plane.store.list_items("ahead").rows == []


async def test_a_control_plane_that_hasnt_heard_of_the_service_is_told_then_asked(
    plane: Plane, tracker: Tracker, tmp_path: Path, clock: FakeClock
) -> None:
    """
    GIVEN: A control plane that started after the broker, so it hasn't had the
           broker's report of its services
     WHEN: a script calls a stub of a service with no key
     THEN: the broker reports its services and asks again, and the call names the
           Connect item, as it does when the report came first
    """
    connections = Connections(
        [tracker.service()],
        CredentialStore(tmp_path / "credentials"),
        plane.broker_socket,
        clock,
    )
    feed = DecisionFeed(plane.broker_socket, tmp_path / "position", [connections.apply])
    broker = Broker(connections, feed)

    refused = await broker.refusal()

    [item] = plane.store.list_items("ahead").rows
    with check:
        assert (refused.code, refused.data["item"]) == ("unavailable", item["id"])
    with check:
        assert [c["service"] for c in plane.store.list_connections()] == ["tracker"]


async def test_a_key_kept_stays_kept_when_the_report_of_it_fails(
    broker: Broker,
    plane: Plane,
    key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    GIVEN: A Connect decision, and a control plane that stops answering reports just
           as the broker keeps the key
     WHEN: the broker applies the decision
     THEN: the key is kept and the outcome says connected; the next report, which
           the broker makes every minute, tells the control plane
    """
    item, _ = plane.store.request_connection("tracker", {"principal": "you"})
    decision = decide(plane, item, "connect", key)

    async def unanswered() -> None:
        raise OSError("the control plane went away")

    monkeypatch.setattr(broker.connections, "report", unanswered)
    await broker.feed.catch_up()

    applied = plane.store.get_decision(decision["id"])["applied"]  # type: ignore[index]
    with check:
        assert applied["outcome"] == "applied"
    with check:
        assert await broker.key() == (key, {"org": "acme"})
