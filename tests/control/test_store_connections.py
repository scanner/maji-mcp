"""
Tests for what the control plane keeps about connections (`src/maji_mcp/control/
store.py`): the services the broker reports, the Connect items built from them, and
when a connection can be disconnected.

The keys themselves never reach the store; `test_passkeys_connect.py` follows a key.
"""

# system imports
from typing import Any

# 3rd party imports
import pytest
from pytest_check import check

# project imports
from maji_mcp.common.protocol import MajiError
from maji_mcp.control.store import ControlStore
from tests.control.factories import ServiceFactory

FINGERPRINT = "sha256:" + "ab" * 32


def connected(service: dict[str, Any], decision: str, item: str) -> dict[str, Any]:
    """A service as the broker reports it once a key from `decision` connected it."""
    return {
        **service,
        "connection": {
            "status": "connected",
            "fingerprint": FINGERPRINT,
            "settings": {"org": "acme"},
            "connected_at": "2027-01-15T08:00:00Z",
            "decision": decision,
            "item": item,
        },
    }


@pytest.mark.parametrize(
    "reported",
    [
        pytest.param({"service": "Tracker"}, id="a name that isn't one"),
        pytest.param({"method": "oauth"}, id="a method not built"),
        pytest.param({"host": ""}, id="no host"),
        pytest.param({"permissions": "issues:read"}, id="permissions as text"),
        pytest.param(
            {"settings": [{"name": "org", "label": "Org", "pattern": "["}]},
            id="a pattern that doesn't compile",
        ),
        pytest.param({"token": "x"}, id="a field it doesn't have"),
        pytest.param({"connection": {"status": "on"}}, id="a connection's status"),
        pytest.param({"connection": {"status": "connected", "key": "k"}}, id="a key"),
    ],
)
def test_a_service_reported_out_of_shape_is_refused(
    store: ControlStore, reported: dict[str, Any]
) -> None:
    """
    GIVEN: A service definition from the broker with one thing out of the contract's
           shape, the key itself in its connection among them
     WHEN: the broker reports it
     THEN: it is refused, and the services reported before stay as they were
    """
    store.report_services([{**ServiceFactory.build(), "connection": None}])

    with pytest.raises(MajiError) as refused:
        store.report_services([{**ServiceFactory.build(), **reported}])

    assert refused.value.code == "invalid_argument"
    assert [c["service"] for c in store.list_connections()] == ["tracker"]


def test_a_connect_item_is_built_from_the_service_and_asked_for_once(
    store: ControlStore, service: dict[str, Any]
) -> None:
    """
    GIVEN: A service the broker reported, not connected
     WHEN: an agent's script needs it, and then you ask for it too, ahead of time
     THEN: one Connect item stands for both: its title, facts and connection come
           from the broker's definition, and only the claim from the agent
      AND: the connections list names it as the item waiting for the service
    """
    agent = {"principal": "claude-code", "session": "ab" * 32}
    first, made = store.request_connection("tracker", agent, "A script needs it.")
    again, made_again = store.request_connection("tracker", {"principal": "you"})

    with check:
        assert (made, made_again, again["id"]) == (True, False, first["id"])
    with check:
        assert first["title"] == "Connect Tracker"
    with check:
        assert first["connection"] == service
    with check:
        assert first["claim"] == "A script needs it."
    with check:
        assert {f["label"]: f["value"] for f in first["facts"]} == {
            "Sent to": "tracker.example",
            "Sent as": "Authorization: Bearer",
            "The key needs": "issues:read, projects:read",
        }
    with check:
        assert store.list_connections() == [
            {
                "service": "tracker",
                "label": "Tracker",
                "method": "api_key",
                "host": "tracker.example",
                "status": "not_connected",
                "item": first["id"],
            }
        ]
    with pytest.raises(MajiError) as missing:
        store.request_connection("nothing", {"principal": "you"})
    assert missing.value.code == "not_found"


def test_a_service_already_connected_is_asked_for_again_to_replace_its_key(
    store: ControlStore, service: dict[str, Any], recorded: Any
) -> None:
    """
    GIVEN: A service connected by a decided Connect item
     WHEN: you ask to connect it again
     THEN: a new Connect item says which key it replaces, by when it was connected
           and the start of its fingerprint
      AND: the earlier item offers Disconnect only while its key is the one in
           effect: once the broker reports a later key, it offers nothing
    """
    first, _ = store.request_connection("tracker", {"principal": "you"})
    decision = recorded(
        first,
        "connect",
        credential_fingerprint=FINGERPRINT,
        credential_settings={"org": "acme"},
    )
    store.report_services([connected(service, decision["id"], first["id"])])
    with check:
        assert [a["id"] for a in store.get_item(first["id"])["actions"]] == [  # type: ignore[index]
            "disconnect"
        ]

    second, _ = store.request_connection("tracker", {"principal": "you"})
    replaces = {f["label"]: f["value"] for f in second["facts"]}["Replaces"]
    with check:
        assert replaces == f"the key connected 2027-01-15T08:00:00Z, {FINGERPRINT[:15]}"

    store.report_services([connected(service, "a-later-decision", second["id"])])
    with check:
        assert store.get_item(first["id"])["actions"] == []  # type: ignore[index]


def test_a_change_to_a_connection_is_an_event_and_a_report_alone_is_not(
    store: ControlStore, service: dict[str, Any]
) -> None:
    """
    GIVEN: A service reported with no connection
     WHEN: the broker reports the same again, then reports it connected
     THEN: only the change is an event, so open clients show it without being told
           of every report
    """
    store.report_services([{**service, "connection": None}])
    store.report_services([connected(service, "d-1", "i-1")])

    changes = store.db.execute(
        "SELECT data FROM events WHERE type = 'connection.changed'"
    ).fetchall()
    assert len(changes) == 1
