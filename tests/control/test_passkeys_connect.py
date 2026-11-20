"""
Tests for connecting a service with a pasted key (`src/maji_mcp/control/passkeys.py`,
with `store.py`, `handoff.py` and the broker's socket in `internal.py`).

You paste the key into a Connect item in your browser. The decision you sign with your
passkey names the key's fingerprint, and the key goes beside it. The control plane
checks the key against the fingerprint, records the decision without the key, and holds
the key in memory until the broker takes it.
"""

# system imports
import hashlib
from collections.abc import AsyncIterator
from typing import Any

# 3rd party imports
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.control.store import ControlStore
from tests.support.contract import fits
from tests.support.control import (
    Control,
    Sockets,
    TabKey,
    Touches,
    choice,
    serving_sockets,
)


@pytest.fixture
async def sockets(store: ControlStore) -> AsyncIterator[Sockets]:
    """The broker's socket, served over the test's database, to take a key from."""
    async with serving_sockets(store) as root:
        yield Sockets(root)


@pytest.fixture
def key(faker: Faker) -> str:
    """A made-up key, as long as a provider's personal token."""
    return "tok_" + faker.password(length=60, special_chars=False)


def fingerprint(key: str) -> str:
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()


@pytest.fixture
async def asked(
    control: Control, passkey: Touches, service: dict[str, Any]
) -> dict[str, Any]:
    """The Connect item for the reported service, asked for from the browser."""
    made = await control.post(f"/connections/{service['service']}/requests", {})
    fits(made, "/connections/{service}/requests", "post")
    assert made.status_code == 201, made.text
    item: dict[str, Any] = made.json()
    return item


def connecting(item: dict[str, Any], key: str, **settings: str) -> dict[str, Any]:
    """The choice that connects the item's service with `key`."""
    return choice(
        item,
        "connect",
        credential_fingerprint=fingerprint(key),
        credential_settings=settings or {"org": "acme"},
    )


async def test_a_pasted_key_reaches_the_broker_once_and_is_kept_nowhere_else(
    control: Control,
    passkey: Touches,
    store: ControlStore,
    sockets: Sockets,
    asked: dict[str, Any],
    service: dict[str, Any],
    key: str,
) -> None:
    """
    GIVEN: A Connect item for a service, asked for from the browser
     WHEN: you paste a key into it and connect, with your passkey, and the broker
           takes the key as it applies the decision
     THEN: the item says where the key will go and how, from the broker's definition
           of the service
      AND: the decision records the key's fingerprint and the settings given with it,
           and the broker gets the key, once
      AND: the key is nowhere in the database, which holds the event stream and is
           what a backup copies, or in any answer the control plane gave, so a
           backup or a client never holds it
    """
    # The item, as the broker defined the service.
    #
    with check:
        assert asked["kind"] == "connect"
    with check:
        assert asked["connection"] == service
    with check:
        assert {f["label"]: f["value"] for f in asked["facts"]}["Sent to"] == (
            "tracker.example"
        )

    # Connecting: the key goes beside the decision the passkey signs.
    #
    answers = []
    decided = await control.decide(
        passkey, connecting(asked, key), secrets={asked["id"]: key}
    )
    answers.append(decided.text)
    fits(decided, "/decisions", "post")
    assert decided.status_code == 201, decided.text
    [decision] = decided.json()["decisions"]
    with check:
        assert decision["credential_fingerprint"] == fingerprint(key)
    with check:
        assert decision["credential_settings"] == {"org": "acme"}

    # The broker takes the key once.
    #
    taken = await sockets.broker.call("take_credential", decision=decision["id"])
    with check:
        assert taken == {"key": key}
    with check:
        assert (
            await sockets.broker.refused("take_credential", decision=decision["id"])
            == "not_found"
        )

    # Nowhere else holds it.
    #
    for path in (
        f"/items/{asked['id']}",
        "/items?lane=behind",
        "/decisions",
        f"/decisions/{decision['id']}",
        "/connections",
    ):
        answers.append((await control.get(path, headers=control.bearer())).text)
    dump = "\n".join(store.db.iterdump())
    with check:
        assert key not in dump
    with check:
        assert not [a for a in answers if key in a]
    with check:
        assert key not in repr(store.keys)


@pytest.mark.parametrize(
    ("pasted", "why"),
    [
        pytest.param("another", "a key that isn't the one signed", id="another key"),
        pytest.param(None, "no key", id="no key"),
        pytest.param("spaced", "a key with a space at its end", id="spaced"),
    ],
)
async def test_a_key_that_isnt_the_one_signed_connects_nothing(
    control: Control,
    passkey: Touches,
    store: ControlStore,
    asked: dict[str, Any],
    key: str,
    faker: Faker,
    pasted: str | None,
    why: str,
) -> None:
    """
    GIVEN: A Connect item, and a decision signed for a key's fingerprint
     WHEN: the request carries a different key, no key, or the key with a space after
           it
     THEN: it is refused, without the key in the answer, and nothing is recorded or
           held: the item still waits
    """
    sent = {
        "another": {asked["id"]: faker.password(length=40, special_chars=False)},
        None: {},
        "spaced": {asked["id"]: key + " "},
    }[pasted]

    refused = await control.decide(passkey, connecting(asked, key), secrets=sent)

    with check:
        assert refused.status_code == 422, why
    with check:
        assert key not in refused.text
    with check:
        assert store.get_item(asked["id"])["state"] == "waiting"  # type: ignore[index]
    with check:
        assert len(store.keys) == 0


async def test_a_key_comes_only_with_a_connect_decision(
    control: Control,
    passkey: Touches,
    queued: Any,
    key: str,
) -> None:
    """
    GIVEN: A changeset waiting in the queue
     WHEN: its approval is sent with a key beside it, or with a fingerprint in it
     THEN: both are refused, since only connecting a service takes a key
    """
    item = queued()

    with_key = await control.decide(passkey, choice(item), secrets={item["id"]: key})
    with_fingerprint = await control.decide(
        passkey, choice(item, credential_fingerprint=fingerprint(key))
    )

    with check:
        assert with_key.status_code == 422
    with check:
        assert with_fingerprint.status_code == 422


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param({}, id="none"),
        pytest.param({"org": "acme", "region": "us"}, id="one it doesn't ask for"),
        pytest.param({"org": "Not a slug!"}, id="one that doesn't match its pattern"),
    ],
)
async def test_a_connect_decision_gives_exactly_the_settings_asked_for(
    control: Control,
    passkey: Touches,
    asked: dict[str, Any],
    key: str,
    settings: dict[str, str],
) -> None:
    """
    GIVEN: A Connect item whose service asks for an organization slug with its key
     WHEN: the decision gives no settings, one it doesn't ask for, or a slug that
           doesn't match the pattern
     THEN: it is refused before anything is signed, naming the setting but never
           quoting what was given
    """
    chosen = choice(
        asked,
        "connect",
        credential_fingerprint=fingerprint(key),
        credential_settings=settings,
    )

    refused = await control.challenge(chosen)

    with check:
        assert refused.status_code == 422
    with check:
        assert "Not a slug!" not in refused.text


async def test_a_connect_decision_takes_the_passkey_and_is_made_on_its_own(
    control: Control,
    passkey: Touches,
    asked: dict[str, Any],
    queued: Any,
    key: str,
) -> None:
    """
    GIVEN: A Connect item, a changeset beside it, and a tab where deciding is unlocked
     WHEN: the connection is decided in one batch with the changeset, or signed by the
           unlocked tab
     THEN: both are refused: connecting a service always takes a passkey touch of its
           own
    """
    batched = await control.challenge(connecting(asked, key), choice(queued()))

    tab = TabKey()
    unlocked = (await control.unlock(passkey, tab)).json()
    signed_by_tab = await control.decide_unlocked(
        tab, unlocked["id"], connecting(asked, key)
    )

    with check:
        assert batched.status_code == 422
    with check:
        assert signed_by_tab.status_code == 403
    with check:
        assert signed_by_tab.json().get("data", {}).get("reason") == "needs_passkey"
