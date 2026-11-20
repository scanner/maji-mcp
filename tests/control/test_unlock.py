"""
Tests for deciding in an unlocked tab: one passkey touch certifies a tab's key, and
the tab then signs each decision itself until it locks.

They go through the REST API, so they cover the unlock routes in `passkeys.py`, the
key checks in `unlock.py`, and the store's record of which tabs are unlocked.
"""

# system imports
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

# 3rd party imports
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.control import canonical, unlock
from maji_mcp.control.store import UNLOCKS_PER_SESSION, ControlStore
from tests.control.factories import (
    PRINCIPAL,
    REPOSITORY,
    MountFactory,
    PromotionFactory,
)
from tests.support.contract import b64, fits, unb64
from tests.support.control import (
    INSTANCE,
    Control,
    TabKey,
    Touches,
    choice,
    serving,
)
from tests.support.services import FakeClock


@pytest.fixture
def tab() -> TabKey:
    """The key pair of one browser tab."""
    return TabKey()


@pytest.fixture
async def unlocked(control: Control, passkey: Touches, tab: TabKey) -> dict[str, Any]:
    """
    The tab, unlocked for deciding by one touch of the passkey: the unlock's id and
    idle limit, as the control plane gave them.
    """
    made = await control.unlock(passkey, tab)
    assert made.status_code == 201
    body: dict[str, Any] = made.json()
    return body


@pytest.fixture
async def elsewhere(store: ControlStore) -> AsyncIterator[Control]:
    """A second browser at the same control plane, with no session yet."""
    async with serving(store) as browser:
        yield browser


async def test_an_unlocked_tab_decides_without_a_passkey_prompt(
    control: Control,
    passkey: Touches,
    tab: TabKey,
    queued: Callable[..., dict[str, Any]],
) -> None:
    """
    GIVEN: A browser signed in with a passkey, and two waiting changesets
     WHEN: the tab unlocks deciding with one passkey touch, then decides each item
           with its own key
     THEN: both decisions land on that one touch; each records the tab it came
           from, and a signature any instance can check: the unlock's challenge
           rebuilds from its nonce, the key's thumbprint and the idle limit, and the
           key's signature covers the decision's challenge
    """
    first, second = queued(), queued()
    before = passkey.touches

    # One touch unlocks the tab, for ten minutes without use.
    #
    made = await control.unlock(passkey, tab)
    assert made.status_code == 201
    unlocked = made.json()
    assert unlocked["idle_seconds"] == 600

    # Each item is decided with the tab's key, and the passkey isn't asked again.
    #
    for item in (first, second):
        decided = await control.decide_unlocked(tab, unlocked["id"], choice(item))
        assert decided.status_code == 201, decided.text
    assert passkey.touches - before == 1

    # The decision says who decided and from which tab.
    #
    [decision] = decided.json()["decisions"]
    proof = decision["signature"]
    certificate = proof["unlock"]
    with check:
        assert decision["by"] == {
            "kind": "you",
            "passkey": passkey.id,
            "unlock": unlocked["id"],
            "instance": INSTANCE,
        }
    with check:
        assert certificate["passkey"] == passkey.id

    # Its signature can be checked by anyone holding the record: the passkey signed
    # the tab's key and idle limit, and the tab's key signed this decision.
    #
    rebuilt = canonical.unlock_challenge(
        certificate["nonce"],
        canonical.key_thumbprint(certificate["key"]),
        certificate["idle_seconds"],
    )
    client_data = json.loads(unb64(certificate["client_data_json"]))
    with check:
        assert unb64(client_data["challenge"]) == rebuilt
    with check:
        assert unlock.verify(
            certificate["key"],
            canonical.challenge(proof["nonce"], proof["batch"]),
            proof["signature"],
        )


async def test_an_unlocked_tab_locks_once_it_goes_unused(
    control: Control,
    clock: FakeClock,
    tab: TabKey,
    unlocked: dict[str, Any],
    queued: Callable[..., dict[str, Any]],
    state_of: Callable[[dict[str, Any]], str],
) -> None:
    """
    GIVEN: A tab that unlocked deciding, with ten minutes' idle limit
     WHEN: it reports that it is in use, then goes unused past the limit
     THEN: each report keeps it unlocked, so it decides twenty minutes after
           unlocking; once unused for longer than the limit it is locked, a decision
           is refused as locked and the item waits, and a report never reopens it
    """
    active = f"/unlocks/{unlocked['id']}/active"

    # Reporting every five minutes keeps the tab unlocked past its ten-minute limit.
    #
    for _ in range(4):
        clock.now += 5 * 60
        kept = await control.post(active, {})
        assert kept.status_code == 204
        fits(kept, "/unlocks/{unlock_id}/active", "post")
    decided = await control.decide_unlocked(tab, unlocked["id"], choice(queued()))
    assert decided.status_code == 201

    # Left unused for longer than the limit, it is locked for good.
    #
    clock.now += 10 * 60 + 1
    later = queued()
    refused = await control.decide_unlocked(tab, unlocked["id"], choice(later))
    assert refused.status_code == 403
    assert refused.json()["data"] == {"reason": "locked"}
    assert state_of(later) == "waiting"
    reopened = await control.post(active, {})
    assert reopened.status_code == 403
    fits(reopened, "/unlocks/{unlock_id}/active", "post")


@pytest.mark.parametrize(
    "remember",
    [
        {"pattern": "p1", "lasts": "permanent"},
        {"pattern": "p1", "lasts": "until", "until": "2099-01-01T00:00:00Z"},
    ],
    ids=["permanently", "until a time"],
)
async def test_a_decision_that_lasts_needs_the_passkey_even_unlocked(
    control: Control,
    passkey: Touches,
    tab: TabKey,
    unlocked: dict[str, Any],
    queued: Callable[..., dict[str, Any]],
    state_of: Callable[[dict[str, Any]], str],
    remember: dict[str, Any],
) -> None:
    """
    GIVEN: An unlocked tab, and a request to mount a repository
     WHEN: the tab approves it remembering the mount past the run or session, signed
           with its key alone
     THEN: it is refused as needing the passkey, and the item waits; the same
           approval with a passkey touch is recorded
    """
    item = queued(MountFactory)
    lasting = choice(item, remember=[remember])

    refused = await control.decide_unlocked(tab, unlocked["id"], lasting)

    assert refused.status_code == 403
    assert refused.json()["data"] == {"reason": "needs_passkey"}
    assert state_of(item) == "waiting"
    assert (await control.decide(passkey, lasting)).status_code == 201


async def test_a_session_grant_ending_at_a_time_is_decided_in_an_unlocked_tab(
    control: Control,
    store: ControlStore,
    clock: FakeClock,
    passkey: Touches,
    tab: TabKey,
    unlocked: dict[str, Any],
    queued: Callable[..., dict[str, Any]],
) -> None:
    """
    GIVEN: An unlocked tab, and a request in an agent's session to mount a repository
     WHEN: the tab approves it for the session, ending in 8 hours at the latest,
           signed with its key alone
     THEN: it is recorded without a passkey touch, since it covers no more than the
           session would
      AND: the grant covers that session until the time, and nothing after it, though
           the session goes on
    """
    item = queued(MountFactory, requester__in_session=True)
    session = item["requester"]["session"]
    until = datetime.fromtimestamp(clock.now + 8 * 3600, UTC).isoformat()
    remember = {"pattern": "p1", "lasts": "session", "until": until}
    touches = passkey.touches

    decided = await control.decide_unlocked(
        tab, unlocked["id"], choice(item, remember=[remember])
    )

    assert decided.status_code == 201, decided.text
    assert passkey.touches == touches
    assert store.grant_covers(PRINCIPAL, "mount", REPOSITORY, session=session)
    clock.now += 8 * 3600 + 1
    assert not store.grant_covers(PRINCIPAL, "mount", REPOSITORY, session=session)


async def test_a_high_risk_decision_needs_the_passkey_even_unlocked(
    control: Control,
    passkey: Touches,
    tab: TabKey,
    unlocked: dict[str, Any],
    queued: Callable[..., dict[str, Any]],
    state_of: Callable[[dict[str, Any]], str],
) -> None:
    """
    GIVEN: An unlocked tab, a high-risk change and a medium-risk one
     WHEN: the tab approves the high-risk change, alone or in a batch with the other,
           signed with its key alone
     THEN: each is refused as needing the passkey, and both items wait; the same
           batch with a passkey touch is recorded
    """
    risky = queued(high_risk=True)
    plain = queued()

    for choices in ([choice(risky)], [choice(plain), choice(risky)]):
        refused = await control.decide_unlocked(tab, unlocked["id"], *choices)
        assert refused.status_code == 403
        assert refused.json()["data"] == {"reason": "needs_passkey"}

    assert state_of(risky) == "waiting"
    assert state_of(plain) == "waiting"
    batch = await control.decide(passkey, choice(plain), choice(risky))
    assert batch.status_code == 201


async def test_a_promotion_in_effect_is_demoted_on_top_of_it_in_an_unlocked_tab(
    control: Control,
    store: ControlStore,
    passkey: Touches,
    tab: TabKey,
    unlocked: dict[str, Any],
    queued: Callable[..., dict[str, Any]],
    faker: Faker,
) -> None:
    """
    GIVEN: A high-risk promotion of a skill, decided with your passkey
     WHEN: the decided item is read, and a tab your passkey unlocked demotes it with a
           note, signed with the tab's key alone
     THEN: the decided promotion offers Demote; the demotion is recorded as a new
           decision naming the promotion it supersedes, and the item stays decided,
           showing both, with nothing more to reconsider
      AND: the broker's feed has the demotion after the promotion
    """
    item = queued(PromotionFactory, high_risk=True)
    made = await control.decide(passkey, choice(item, "promote"))
    promoted = made.json()["decisions"][0]
    note = faker.sentence()

    # Once promoted, the item offers one thing: to demote the skill again.
    #
    fetched = await control.get(f"/items/{item['id']}")
    fits(fetched, "/items/{item_id}", "get")
    decided = fetched.json()
    with check:
        assert decided["actions"] == [
            {"id": "demote", "label": "Demote", "style": "danger"}
        ]
    with check:
        assert decided["superseded"] is None

    # Taking something back needs no passkey, even for a high-risk item: the tab's
    # key signs the demotion, which names the promotion it is recorded on top of.
    #
    made = await control.decide_unlocked(
        tab,
        unlocked["id"],
        choice(decided, "demote", supersedes=promoted["id"], note=note),
    )
    assert made.status_code == 201
    demotion = made.json()["decisions"][0]
    with check:
        assert demotion["supersedes"] == promoted["id"]
    with check:
        assert demotion["by"]["unlock"] == unlocked["id"]

    # The item stays decided, showing the demotion and the promotion it took back,
    # and offers nothing more.
    #
    fetched = await control.get(f"/items/{item['id']}")
    fits(fetched, "/items/{item_id}", "get")
    after = fetched.json()
    with check:
        assert after["state"] == "decided"
    with check:
        assert after["decision"]["id"] == demotion["id"]
    with check:
        assert after["decision"]["note"] == note
    with check:
        assert after["superseded"]["id"] == promoted["id"]
    with check:
        assert after["actions"] == []

    # The broker reads both decisions, in the order they were made.
    #
    feed = store.decisions_since(0)["decisions"]
    with check:
        assert [d["decision"]["action"] for d in feed] == ["promote", "demote"]


async def test_an_unlock_belongs_to_its_tabs_session(
    control: Control,
    elsewhere: Control,
    passkey: Touches,
    tab: TabKey,
    unlocked: dict[str, Any],
    queued: Callable[..., dict[str, Any]],
    state_of: Callable[[dict[str, Any]], str],
) -> None:
    """
    GIVEN: A tab unlocked in one signed-in session
     WHEN: another session signs the challenge with the same key and names the
           unlock, or the first session locks the tab or signs out
     THEN: the other session is refused as locked; after locking, or after signing
           out, the tab's own decisions are refused too
    """
    item = queued()
    approval = choice(item)
    assert (await elsewhere.sign_in(passkey)).status_code == 204

    # Another session holding the tab's key can't use the tab's unlock.
    #
    stolen = await elsewhere.decide_unlocked(tab, unlocked["id"], approval)
    with check:
        assert stolen.status_code == 403
    with check:
        assert stolen.json()["data"] == {"reason": "locked"}

    # An unlock that its own session locks decides nothing afterwards.
    #
    again = (await elsewhere.unlock(passkey, tab)).json()
    locked = await elsewhere.delete(f"/unlocks/{again['id']}")
    assert locked.status_code == 204
    fits(locked, "/unlocks/{unlock_id}", "delete")
    refused = await elsewhere.decide_unlocked(tab, again["id"], approval)
    with check:
        assert refused.json()["data"] == {"reason": "locked"}

    # Signing out locks the session's tabs, and signing in again doesn't reopen
    # them.
    #
    assert (await control.delete("/session")).status_code == 204
    assert (await control.sign_in(passkey)).status_code == 204
    after = await control.decide_unlocked(tab, unlocked["id"], approval)
    with check:
        assert after.json()["data"] == {"reason": "locked"}
    with check:
        assert state_of(item) == "waiting"


async def test_a_session_keeps_only_its_newest_unlocked_tabs(
    control: Control, passkey: Touches, clock: FakeClock
) -> None:
    """
    GIVEN: A session that has unlocked as many tabs as it may hold
     WHEN: it unlocks one more
     THEN: the oldest tab locks, and the rest stay unlocked
    """
    ids = []
    for _ in range(UNLOCKS_PER_SESSION + 1):
        ids.append((await control.unlock(passkey, TabKey())).json()["id"])
        clock.now += 1

    oldest = await control.post(f"/unlocks/{ids[0]}/active", {})
    assert oldest.status_code == 403
    for unlock_id in ids[1:]:
        kept = await control.post(f"/unlocks/{unlock_id}/active", {})
        assert kept.status_code == 204


async def test_a_signature_from_another_key_decides_nothing(
    control: Control,
    unlocked: dict[str, Any],
    queued: Callable[..., dict[str, Any]],
    state_of: Callable[[dict[str, Any]], str],
) -> None:
    """
    GIVEN: An unlocked tab
     WHEN: a decision names its unlock but is signed by a different key
     THEN: it is refused, and the item waits
    """
    item = queued()

    forged = await control.decide_unlocked(TabKey(), unlocked["id"], choice(item))

    assert forged.status_code == 403
    assert "data" not in forged.json()
    assert state_of(item) == "waiting"


@pytest.mark.parametrize(
    "change",
    [
        {"d": "AAAA"},
        {"crv": "P-384"},
        {"kty": "RSA"},
        {"x": b64(bytes(32))},
        {"y": b64(bytes(31))},
    ],
    ids=[
        "a private key",
        "another curve",
        "another type",
        "off the curve",
        "too short",
    ],
)
async def test_a_tab_key_is_a_p256_public_key(
    control: Control, passkey: Touches, tab: TabKey, change: dict[str, str]
) -> None:
    """
    GIVEN: A signed-in browser
     WHEN: it asks to unlock with a key that is private, on another curve, of
           another type, not a point on P-256, or the wrong length
     THEN: it is refused before any challenge
    """
    refused = await control.post("/unlocks/options", {"key": {**tab.jwk, **change}})
    assert refused.status_code == 422
    fits(refused, "/unlocks/options", "post")


async def test_a_token_never_unlocks(
    control: Control, passkey: Touches, tab: TabKey
) -> None:
    """
    GIVEN: A client with a token, as an agent's shell could have
     WHEN: it asks to unlock deciding
     THEN: it is refused: only a browser signed in with a passkey unlocks
    """
    refused = await control.post(
        "/unlocks/options", {"key": tab.jwk}, headers=control.bearer()
    )
    assert refused.status_code == 403
    fits(refused, "/unlocks/options", "post")
