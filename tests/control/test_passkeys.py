"""
Tests for the routes that need a passkey (`passkeys.py`), through the REST API:
registering passkeys, signing in, and deciding, with what an approval may remember.

A software authenticator (`tests/support/contract.py`) registers and signs as a
platform passkey does, so py_webauthn's own checks run. Whether a choice fits its
item is the store's rule (`ControlStore.check_choice`); it is tested here because a
client meets it as the answer to asking for a challenge. Deciding from an unlocked
tab is in `test_unlock.py`.
"""

# system imports
from collections.abc import Callable
from typing import Any

# 3rd party imports
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.common.names import session_name
from maji_mcp.control import canonical
from maji_mcp.control.store import ControlStore
from tests.control.factories import (
    PRINCIPAL,
    REPOSITORY,
    MountFactory,
    MountsFactory,
    PatternFactory,
    PromotionFactory,
    RequesterFactory,
)
from tests.support.contract import Authenticator, fits, unb64
from tests.support.control import INSTANCE, ORIGIN, Control, Touches, choice
from tests.support.services import FakeClock

###############################################################################
# Bring-up and signing in


async def test_the_first_passkey_takes_the_setup_code_once(control: Control) -> None:
    """
    GIVEN: A new instance, with no passkey yet
     WHEN: a passkey is registered without the setup code, then with it, then a second
           one tries the same code
     THEN: health says setup is needed until the first passkey is in; the first try is
           refused, the second registers it and returns ten recovery codes once, and
           the spent code registers nothing more
    """
    health = await control.get("/health")
    fits(health, "/health", "get")
    assert health.json()["setup_needed"] is True

    refused = await control.register(Authenticator())
    assert refused.status_code == 403
    fits(refused, "/passkeys", "post")

    first = await control.register(Authenticator(), setup_code=control.setup_code)
    fits(first, "/passkeys", "post")
    assert first.status_code == 201
    assert len(first.json()["recovery_codes"]) == 10
    assert (await control.get("/health")).json()["setup_needed"] is False

    again = await control.register(Authenticator(), setup_code=control.setup_code)
    assert again.status_code == 403
    fits(again, "/passkeys", "post")


async def test_a_recovery_code_adds_a_passkey_once(control: Control) -> None:
    """
    GIVEN: An instance whose first passkey came with recovery codes
     WHEN: a new device registers a passkey with one of them, then another device
           tries the same code
     THEN: the first registers and gets no recovery codes of its own; the second is
           refused
    """
    first = await control.register(Authenticator(), setup_code=control.setup_code)
    code = first.json()["recovery_codes"][0]

    recovered = await control.register(Authenticator(), "phone", recovery_code=code)
    assert recovered.status_code == 201
    fits(recovered, "/passkeys", "post")
    assert "recovery_codes" not in recovered.json()

    spent = await control.register(Authenticator(), recovery_code=code)
    assert spent.status_code == 403
    fits(spent, "/passkeys", "post")


@pytest.mark.parametrize("attachment", ["platform", None])
async def test_the_setup_pages_registration_shape_is_accepted(
    control: Control, attachment: str | None
) -> None:
    """
    GIVEN: The credential JSON the setup page builds in a browser, with transports,
           the authenticator's attachment (or null where the browser gives none) and
           the browser's extension results
     WHEN: it registers the first passkey with the setup code
     THEN: the passkey is registered
    """
    options = await control.post("/passkeys/registration/options", {"name": "this Mac"})
    body = options.json()
    credential = Authenticator().register(body["public_key"], ORIGIN)
    credential["response"]["transports"] = ["internal", "hybrid"]
    credential["authenticatorAttachment"] = attachment
    credential["clientExtensionResults"] = {"credProps": {"rk": True}}

    made = await control.post(
        "/passkeys",
        {
            "registration_id": body["registration_id"],
            "credential": credential,
            "setup_code": control.setup_code,
        },
    )

    assert made.status_code == 201, made.text


async def test_signing_in_sets_a_strict_cookie(control: Control) -> None:
    """
    GIVEN: A registered passkey
     WHEN: the web app signs in with it
     THEN: it gets a session cookie that scripts can't read and other sites can't
           send, and that session can read the queue
    """
    key = Authenticator()
    await control.register(key, setup_code=control.setup_code)

    signed = await control.sign_in(key)

    fits(signed, "/session", "post")
    cookie = signed.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert (await control.get("/items", params={"lane": "ahead"})).status_code == 200


async def test_adding_or_removing_a_passkey_needs_a_fresh_sign_in(
    control: Control, passkey: Touches, clock: FakeClock
) -> None:
    """
    GIVEN: A session signed in more than five minutes ago
     WHEN: it tries to add a passkey, or remove one
     THEN: both are refused until it signs in again; then the second passkey can be
           added and removed, and the last one can't be removed
    """
    clock.now += 6 * 60
    stale_add = await control.register(Authenticator(), "phone")
    assert stale_add.status_code == 403
    fits(stale_add, "/passkeys", "post")
    stale_remove = await control.delete(f"/passkeys/{passkey.id}")
    assert stale_remove.status_code == 403
    fits(stale_remove, "/passkeys/{passkey_id}", "delete")

    await control.sign_in(passkey)
    phone = Authenticator()
    assert (await control.register(phone, "phone")).status_code == 201
    removed = await control.delete(f"/passkeys/{phone.id}")
    assert removed.status_code == 204
    fits(removed, "/passkeys/{passkey_id}", "delete")
    last = await control.delete(f"/passkeys/{passkey.id}")
    assert last.status_code == 409
    fits(last, "/passkeys/{passkey_id}", "delete")


###############################################################################
# Deciding


async def test_a_decision_needs_a_passkey_and_lands_once(
    control: Control, passkey: Touches, queued: Callable[..., dict[str, Any]]
) -> None:
    """
    GIVEN: A waiting changeset, and a client with only a token
     WHEN: the client gets a challenge for approving it, the passkey signs it, and
           the decision is sent
     THEN: the decision is recorded with its signature, the item moves behind, and the
           challenge the passkey signed rebuilds from the nonce and decision hashes
           the response gave, so a client can check it signed what it showed
    """
    item = queued()

    # The challenge is the hash of the nonce and the decision hashes the client was
    # given with it, so the client can rebuild what the passkey is about to sign.
    #
    body = (await control.challenge(choice(item))).json()
    rebuilt = canonical.challenge(body["nonce"], body["decision_hashes"])
    assert rebuilt == unb64(body["public_key"]["challenge"])

    # Signed by the passkey, the decision is recorded as yours, with the passkey
    # that signed it.
    #
    assertion = passkey.sign(body["public_key"]["challenge"], ORIGIN)
    decided = await control.send(body["challenge_id"], assertion=assertion)
    fits(decided, "/decisions", "post")
    assert decided.status_code == 201
    [decision] = decided.json()["decisions"]
    with check:
        assert decision["signature"]["passkey"] == passkey.id
    with check:
        assert decision["by"] == {
            "kind": "you",
            "passkey": passkey.id,
            "instance": INSTANCE,
        }

    # The item is behind you now, and its decision is in the history.
    #
    behind = await control.get(
        "/items", params={"lane": "behind"}, headers=control.bearer()
    )
    assert behind.json()["items"][0]["state"] == "decided"
    history = await control.get("/decisions", headers=control.bearer())
    fits(history, "/decisions", "get")
    one = await control.get(f"/decisions/{decision['id']}", headers=control.bearer())
    fits(one, "/decisions/{decision_id}", "get")


@pytest.mark.parametrize(
    ("proof", "status"),
    [({}, 422), ({"unlock": {"id": "anything", "signature": "AAAA"}}, 403)],
    ids=["no proof", "a signature said to be an unlocked tab's"],
)
async def test_a_token_alone_never_decides(
    control: Control,
    passkey: Touches,
    queued: Callable[..., dict[str, Any]],
    state_of: Callable[[dict[str, Any]], str],
    proof: dict[str, Any],
    status: int,
) -> None:
    """
    GIVEN: A client with a token, as an agent's shell could have
     WHEN: it gets a challenge and sends the decision with no proof, or with a
           signature claiming to come from an unlocked tab
     THEN: it is refused, and the item still waits
    """
    item = queued()
    issued = (await control.challenge(choice(item))).json()

    refused = await control.send(issued["challenge_id"], **proof)

    assert refused.status_code == status
    fits(refused, "/decisions", "post")
    assert state_of(item) == "waiting"


@pytest.mark.parametrize(
    ("counter", "signing"),
    [
        (False, {"origin": "http://localhost:9999"}),
        (False, {"verified": False}),
        (True, {"repeat_count": True}),
    ],
    ids=["another localhost port", "user not verified", "counter went back"],
)
async def test_an_assertion_that_doesnt_hold_decides_nothing(
    control: Control,
    queued: Callable[..., dict[str, Any]],
    state_of: Callable[[dict[str, Any]], str],
    counter: bool,
    signing: dict[str, Any],
) -> None:
    """
    GIVEN: A waiting changeset
     WHEN: the assertion comes from a page on another localhost port, or without the
           user verified, or from a key whose counter didn't go up
     THEN: the decision is refused with 403, and the item still waits
    """
    # Only a key that keeps a counter, as a hardware key does, can show one failing
    # to go up, and it needs a count to repeat, so each key decides one item first.
    #
    key = Authenticator(counter=counter)
    registered = await control.register(key, setup_code=control.setup_code)
    assert registered.status_code == 201
    assert (await control.decide(key, choice(queued()))).status_code == 201
    item = queued()

    refused = await control.decide(key, choice(item), **signing)

    assert refused.status_code == 403
    fits(refused, "/decisions", "post")
    assert state_of(item) == "waiting"


@pytest.mark.parametrize(
    ("uses", "age"),
    [(1, 0), (0, 6 * 60)],
    ids=["used already", "more than five minutes old"],
)
async def test_a_challenge_decides_once_and_only_while_fresh(
    control: Control,
    passkey: Touches,
    clock: FakeClock,
    queued: Callable[..., dict[str, Any]],
    uses: int,
    age: int,
) -> None:
    """
    GIVEN: A challenge for approving a waiting changeset, and the passkey's
           assertion over it
     WHEN: the decision is sent again after it has landed, or sent for the first
           time more than five minutes after the challenge was issued
     THEN: it is refused with 410, as a replay or as stale
    """
    issued = (await control.challenge(choice(queued()))).json()
    assertion = passkey.sign(issued["public_key"]["challenge"], ORIGIN)

    # Either the challenge is spent on a decision that lands, or the clock moves on
    # past it.
    #
    for _ in range(uses):
        landed = await control.send(issued["challenge_id"], assertion=assertion)
        assert landed.status_code == 201
    clock.now += age

    refused = await control.send(issued["challenge_id"], assertion=assertion)

    assert refused.status_code == 410
    fits(refused, "/decisions", "post")


async def test_an_item_decided_meanwhile_is_a_conflict(
    control: Control,
    passkey: Touches,
    store: ControlStore,
    queued: Callable[..., dict[str, Any]],
) -> None:
    """
    GIVEN: Two challenges for the same item, as from two clients
     WHEN: both are signed, and the second is sent after the first landed
     THEN: the second is refused with 409, naming the item's state now, and only one
           decision is recorded
    """
    item = queued()
    first, second = [
        (await control.challenge(choice(item, action))).json()
        for action in ("approve", "reject")
    ]

    for issued, status in ((first, 201), (second, 409)):
        assertion = passkey.sign(issued["public_key"]["challenge"], ORIGIN)
        response = await control.send(issued["challenge_id"], assertion=assertion)
        assert response.status_code == status
        fits(response, "/decisions", "post")

    assert response.json()["data"]["items"][0]["state"] == "decided"
    assert len(store.list_decisions(item_id=item["id"]).rows) == 1


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"action": "promote"}, 422),
        ({"action": "return"}, 422),
        ({"content_hash": "sha256:" + "0" * 64}, 409),
    ],
    ids=[
        "an action it doesn't offer",
        "a return without its note",
        "another content hash",
    ],
)
async def test_a_choice_must_fit_its_item(
    control: Control,
    passkey: Touches,
    queued: Callable[..., dict[str, Any]],
    change: dict[str, str],
    status: int,
) -> None:
    """
    GIVEN: A waiting changeset
     WHEN: a challenge is asked for with an action it doesn't offer, a return without
           its note, or a content hash other than the item's
     THEN: the first two are refused with 422 and the last with 409, and no challenge
           is issued
    """
    item = queued()

    response = await control.challenge({**choice(item), **change})

    assert response.status_code == status
    fits(response, "/decisions/challenge", "post")
    assert "challenge_id" not in response.json()


###############################################################################
# Remembering an approval


async def test_a_remembered_approval_becomes_a_grant(
    control: Control,
    passkey: Touches,
    store: ControlStore,
    queued: Callable[..., dict[str, Any]],
) -> None:
    """
    GIVEN: A request to mount a repository, offering a pattern for later mounts of it,
           and another pattern for a write
     WHEN: the approval remembers the mount pattern for the session
     THEN: one touch records the decision and a grant for the requester, named by its
           session's name, and a choice remembering the write pattern is refused
           before any challenge
    """
    item = queued(
        MountFactory,
        requester__in_session=True,
        patterns=[
            PatternFactory.build(),
            PatternFactory.build(id="p2", access="write", resource="task-demo"),
        ],
    )
    session = item["requester"]["session"]

    # A write of content the requester wrote comes to the queue each time, so its
    # pattern can't be remembered.
    #
    write = await control.challenge(
        choice(item, remember=[{"pattern": "p2", "lasts": "session"}])
    )
    assert write.status_code == 422
    fits(write, "/decisions/challenge", "post")

    # Remembering the mount for the session makes one grant with the one decision.
    #
    touches = passkey.touches
    decided = await control.decide(
        passkey, choice(item, remember=[{"pattern": "p1", "lasts": "session"}])
    )
    [decision] = decided.json()["decisions"]
    [granted] = decision["granted"]
    assert passkey.touches - touches == 1

    # The grant is the requester's, on what the pattern covers, held by the session
    # the request came from.
    #
    grant = store.get_grant(granted)
    assert grant is not None
    with check:
        assert grant["principal"] == PRINCIPAL
    with check:
        assert grant["pattern"]["resource"] == REPOSITORY
    with check:
        assert grant["lasts"] == "session" and grant["session"] == session
    with check:
        assert grant["session_name"] == session_name(session)


@pytest.mark.parametrize(
    ("action", "remember", "named"),
    [
        ("deny", {"pattern": "p1", "lasts": "permanent"}, {}),
        ("approve", {"pattern": "p1", "lasts": "until"}, {}),
        (
            "approve",
            {"pattern": "p1", "lasts": "until", "until": "2020-01-01T00:00:00Z"},
            {},
        ),
        (
            "approve",
            {"pattern": "p1", "lasts": "permanent", "until": "2099-01-01T00:00:00Z"},
            {},
        ),
        (
            "approve",
            {"pattern": "p1", "lasts": "run", "until": "2099-01-01T00:00:00Z"},
            {"in_run": True},
        ),
        (
            "approve",
            {"pattern": "p1", "lasts": "session", "until": "2020-01-01T00:00:00Z"},
            {"in_session": True},
        ),
        ("approve", {"pattern": "p1", "lasts": "run"}, {"in_session": True}),
        ("approve", {"pattern": "p1", "lasts": "session"}, {}),
        ("approve", {"pattern": "p1", "lasts": "forever"}, {}),
    ],
    ids=[
        "a refusal",
        "until without a time",
        "until in the past",
        "a time without until",
        "a time on a run",
        "a session ending in the past",
        "a run nobody named",
        "a session nobody named",
        "an unknown duration",
    ],
)
async def test_a_remembered_approval_must_be_one_that_can_lapse(
    control: Control,
    passkey: Touches,
    queued: Callable[..., dict[str, Any]],
    action: str,
    remember: dict[str, Any],
    named: dict[str, bool],
) -> None:
    """
    GIVEN: A request to mount a repository
     WHEN: a decision on it asks to remember a refusal, or for a duration the grant
           couldn't be held to: a time missing or past, or a run or session the
           requester didn't name
     THEN: it is refused with 422 before any challenge, so no grant is made that
           never lapses or that a refusal created
    """
    # `named` says which of a session and a run the requester gave with its request.
    #
    item = queued(MountFactory, requester=RequesterFactory.build(**named))

    refused = await control.challenge(choice(item, action, remember=[remember]))

    assert refused.status_code == 422
    fits(refused, "/decisions/challenge", "post")


def _remember(*mounts: str) -> list[dict[str, str]]:
    """
    What a choice remembers of a request for several mounts: `api` is the mount of
    api for the session, and `api:run` the same for the run.
    """
    kept = [mount.partition(":") for mount in mounts]
    return [
        {"pattern": f"mount-{name}", "lasts": lasts or "session"}
        for name, _, lasts in kept
    ]


WITHOUT_NOTES = {"api": "approve", "web": "approve", "notes": "deny"}


@pytest.mark.parametrize(
    ("parts", "remember", "status"),
    [
        (None, None, 422),
        (None, _remember("api:run", "web", "notes"), 422),
        (None, _remember("api", "web"), 422),
        (WITHOUT_NOTES, _remember("api"), 422),
        (WITHOUT_NOTES, _remember("api", "web"), 200),
        (None, _remember("api", "web", "notes"), 200),
        (WITHOUT_NOTES, _remember("notes:run"), 422),
        (None, _remember("api:run", "api:permanent"), 422),
        (None, [*_remember("api:run"), "mount-web"], 422),
        (None, [], 422),
    ],
    ids=[
        "none remembered",
        "one remembered only for the run",
        "one approved and not remembered",
        "one of two approved not remembered",
        "each approved one remembered",
        "all remembered",
        "a refused part's pattern",
        "a pattern twice",
        "a pattern not given as one",
        "an empty list",
    ],
)
async def test_an_approval_of_mounts_remembers_each_one_and_nothing_else(
    control: Control,
    passkey: Touches,
    queued: Callable[..., dict[str, Any]],
    parts: dict[str, str] | None,
    remember: list[Any] | None,
    status: int,
) -> None:
    """
    GIVEN: A request to mount three repositories, with a pattern for each
     WHEN: an approval is checked before its challenge
     THEN: If it approves a repository without remembering its mount, or remembers
           it only for the run, it is refused with 422: run_python returns as soon as
           it asks, so a mount approved just once, or for a run that has ended, would
           cover no run at all
      AND: If it asks to remember the pattern of a repository it refuses, the same
           pattern twice, an entry that isn't a pattern, or an empty list, it is
           refused with 422
      AND: If it remembers every repository it approves, once each, it is accepted,
           and a repository it refuses needs nothing
      AND: asking makes no grant either way
    """
    item = queued(MountsFactory)

    answer = await control.challenge(choice(item, parts=parts, remember=remember))

    assert answer.status_code == status, answer.text
    fits(answer, "/decisions/challenge", "post")
    grants = await control.get("/grants", headers=control.bearer())
    assert grants.json()["grants"] == []


async def test_each_part_is_remembered_on_its_own(
    control: Control,
    passkey: Touches,
    store: ControlStore,
    queued: Callable[..., dict[str, Any]],
) -> None:
    """
    GIVEN: A request to mount three repositories, with a pattern for each
     WHEN: the approval refuses one repository, remembers another for the session
           and the third permanently
     THEN: one decision makes a grant for each remembered repository, each lasting
           as chosen, and a later mount is covered repository by repository: the
           two remembered ones are, the refused one isn't
    """
    item = queued(MountsFactory)
    asked = {k: item["requester"][k] for k in ("session", "run")}

    decided = await control.decide(
        passkey,
        choice(item, parts=WITHOUT_NOTES, remember=_remember("api", "web:permanent")),
    )

    # The one decision makes a grant for each repository it remembers, lasting as
    # chosen for that repository.
    #
    assert decided.status_code == 201, decided.text
    [decision] = decided.json()["decisions"]
    grants = [store.get_grant(g) for g in decision["granted"]]
    assert [(g["pattern"]["resource"], g["lasts"]) for g in grants if g] == [
        ("~/src/api", "session"),
        ("~/src/web", "permanent"),
    ]

    # A later mount from the same session and run is covered for those two
    # repositories, and for the one you refused by nothing.
    #
    for resource, covered in (
        ("~/src/api", True),
        ("~/src/web", True),
        ("~/src/notes", False),
    ):
        found = store.grant_covers(PRINCIPAL, "mount", resource, **asked)
        with check:
            assert (found is not None) is covered, resource


###############################################################################
# Reconsidering a decision: demoting a skill


async def test_a_demotion_takes_back_and_nothing_else(
    control: Control,
    passkey: Touches,
    store: ControlStore,
    queued: Callable[..., dict[str, Any]],
    recorded: Callable[..., dict[str, Any]],
    faker: Faker,
) -> None:
    """
    GIVEN: A decided promotion, a waiting item and a decided approval
     WHEN: demotions are asked for that carry more than a note, name a decision that
           isn't the item's latest, name one on a waiting item, or reconsider an
           approval
     THEN: each is refused before anything is recorded: carrying more than a note or
           naming nothing to reconsider with 422, the rest with 409
      AND: no grant is made, and the promotion is still the item's decision
    """
    promotion = queued(PromotionFactory)
    promoted = recorded(promotion, "promote")["id"]
    waiting = queued()
    approval = queued()
    approved = recorded(approval)["id"]
    another = f"{INSTANCE}-{faker.sha1()}"

    # Each demotion asks for more than taking back the item's latest decision, or
    # has nothing to take back.
    #
    refusals = {
        "carrying more than a note": (
            choice(
                promotion,
                "demote",
                supersedes=promoted,
                remember=[{"pattern": "p1", "lasts": "permanent"}],
            ),
            422,
        ),
        "naming another decision": (
            choice(promotion, "demote", supersedes=another),
            409,
        ),
        "on a waiting item": (choice(waiting, "demote", supersedes=another), 422),
        "of an approval": (choice(approval, "demote", supersedes=approved), 409),
    }

    # The refusals don't depend on one another, so each is reported on its own.
    #
    for case, (demotion, status) in refusals.items():
        refused = await control.decide(passkey, demotion)
        with check:
            assert refused.status_code == status, (case, refused.json())

    # None of them recorded anything.
    #
    assert store.list_grants().rows == []
    found = store.get_item(promotion["id"])
    assert found is not None and found["decision"]["id"] == promoted
