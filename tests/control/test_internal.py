"""
Tests for the control plane's internal sockets: one for the broker, one for the
gateway.

They run over real Unix sockets, as the services will.
"""

# system imports
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

# 3rd party imports
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.common.protocol import MajiError
from maji_mcp.control import canonical
from maji_mcp.control.store import ControlStore
from tests.control.factories import (
    PRINCIPAL,
    REPOSITORY,
    ChangesetFactory,
    ItemFactory,
    MountsFactory,
    PatternFactory,
    RequesterFactory,
)
from tests.support.control import INSTANCE, Sockets, serving_sockets
from tests.support.services import FakeClock


@pytest.fixture
def requester() -> dict[str, str]:
    """The agent the broker raises items for: its principal, session and run."""
    agent: dict[str, str] = RequesterFactory.build(in_session=True, in_run=True)
    return agent


@pytest.fixture
async def sockets(store: ControlStore) -> AsyncIterator[Sockets]:
    """The broker's socket and the gateway's, served over the test's database."""
    async with serving_sockets(store) as root:
        yield Sockets(root)


@pytest.fixture
def raised(
    sockets: Sockets, requester: dict[str, str]
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """
    A function that has the broker raise an item for the agent through its socket,
    and returns it: a changeset, or what the factory given builds.
    """

    async def raised(
        of: type[ItemFactory] = ChangesetFactory, **changes: Any
    ) -> dict[str, Any]:
        item: dict[str, Any] = await sockets.broker.call(
            "create_item", **of.build(requester=requester, **changes)
        )
        return item

    return raised


@pytest.fixture
def mount_granted(store: ControlStore, requester: dict[str, str]) -> Callable[..., str]:
    """
    A function that grants the agent a mount straight into the database, held to its
    run or its session or kept as `lasts` says, and returns the grant's id.
    """

    def mount_granted(
        lasts: str, resource: str = REPOSITORY, until: str | None = None
    ) -> str:
        # A mount is never remembered for a run through a decision, so the grant is
        # written as the store writes one, under the requester's run and session.
        #
        return store._add_grant(
            requester,
            PatternFactory.build(resource=resource),
            {"lasts": lasts, "until": until},
            None,
            store.now(),
        )

    return mount_granted


@pytest.fixture
def covers(sockets: Sockets) -> Callable[..., Awaitable[str | None]]:
    """
    A function that asks, as the broker does, which grant lets the agent mount a
    path, and returns the grant's id or None.
    """

    async def covers(resource: str = REPOSITORY, **asked: Any) -> str | None:
        found = await sockets.broker.call(
            "grant_covers",
            principal=PRINCIPAL,
            access="mount",
            resource=resource,
            **asked,
        )
        grant: str | None = found["grant"]
        return grant

    return covers


###############################################################################
# What each caller can do


@pytest.mark.parametrize(
    ("caller", "method"),
    [
        ("broker", "record_decisions"),
        ("broker", "decide"),
        ("broker", "submit_question"),
        ("broker", "item_status"),
        ("broker", "_flag"),
        ("gateway", "status_of"),
        ("gateway", "record_decisions"),
        ("gateway", "create_item"),
        ("gateway", "decisions_since"),
        ("gateway", "record_outcome"),
        ("gateway", "grant_covers"),
        ("gateway", "handle"),
        ("gateway", "__init__"),
    ],
)
async def test_each_socket_offers_only_its_callers_methods(
    sockets: Sockets, caller: str, method: str
) -> None:
    """
    GIVEN: The broker's socket and the gateway's socket
     WHEN: either is asked for a method that decides, a method that belongs to the
           other caller, or one of the handler's own helpers
     THEN: the socket has no such method: nothing decides through either, the
           gateway can't create items with facts or read decisions in bulk, and the
           broker can't act for an agent
    """
    socket = sockets.broker if caller == "broker" else sockets.gateway
    with pytest.raises(MajiError, match=f"no method {method}"):
        await socket.call(method)


###############################################################################
# The broker's socket


async def test_an_item_the_broker_makes_waits_in_the_queue(
    store: ControlStore, sockets: Sockets, requester: dict[str, str]
) -> None:
    """
    GIVEN: The broker with a changeset to propose
     WHEN: it creates the item through its socket
     THEN: the item waits ahead, with the requester and facts it gave, and its own id
           and content hash
    """
    given = ChangesetFactory.build(requester=requester)

    item = await sockets.broker.call("create_item", **given)

    with check:
        assert item["state"] == "waiting"
    with check:
        assert item["requester"] == requester
    with check:
        assert item["facts"] == given["facts"]
    with check:
        assert item["id"].startswith(f"{INSTANCE}-")
    with check:
        assert item["content_hash"] == canonical.content_hash(item)
    with check:
        assert store.list_items("ahead").rows[0]["id"] == item["id"]


@pytest.mark.parametrize(
    "params",
    [
        {"kind": "nonsense"},
        {"requester": {"session": "s-1"}},
        {"requester": {"principal": "p", "role": "admin"}},
        {"requester": {"principal": "p", "client": "claude-code"}},
        {"requester": {"principal": "p", "client": {"name": "x", "os": "y"}}},
        {"facts": [{"label": "only a label"}]},
        {"state": "decided"},
        {"id": "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b8n"},
        {"blocking": "false"},
        {"expires_at": "2027-01-15T10:00:00"},
    ],
    ids=[
        "kind",
        "no principal",
        "unknown requester field",
        "a client that isn't a name",
        "a client with more than a name and version",
        "bad fact",
        "a state",
        "an id",
        "blocking as text",
        "a time without its offset",
    ],
)
async def test_an_item_that_doesnt_fit_is_refused(
    store: ControlStore,
    sockets: Sockets,
    requester: dict[str, str],
    params: dict[str, Any],
) -> None:
    """
    GIVEN: A request to create an item with an unknown kind, a requester without a
           principal or with extra fields, a malformed fact, a field the control
           plane sets itself, blocking as text, or a time without its offset
     WHEN: it reaches the broker's socket
     THEN: it is refused with invalid_argument, and nothing is queued
    """
    fitting = ChangesetFactory.build(requester=requester)

    code = await sockets.broker.refused("create_item", **{**fitting, **params})

    assert code == "invalid_argument"
    assert store.list_items("ahead").rows == []


async def test_the_broker_applies_decisions_in_order_and_reports_outcomes(
    sockets: Sockets,
    raised: Callable[..., Awaitable[dict[str, Any]]],
    recorded: Callable[..., dict[str, Any]],
    faker: Faker,
) -> None:
    """
    GIVEN: Two decided items
     WHEN: the broker reads the decisions after position 0, records an outcome for
           the first, then reads after the position it was given
     THEN: it gets both decisions in order, each with its item; the second read is
           empty; and the requester's status shows the decision and its outcome
    """
    first, second = await raised(), await raised()
    recorded(first)
    recorded(second)

    read = await sockets.broker.call("decisions_since", position=0)
    assert [d["item"]["id"] for d in read["decisions"]] == [first["id"], second["id"]]
    decision = read["decisions"][0]["decision"]
    await sockets.broker.call(
        "record_outcome",
        decision=decision["id"],
        outcome="applied",
        detail=faker.sentence(),
    )
    later = await sockets.broker.call("decisions_since", position=read["position"])
    assert later["decisions"] == []

    status = await sockets.gateway.call(
        "item_status", item=first["id"], principal=PRINCIPAL
    )
    assert status["decision"]["action"] == "approve"
    assert status["outcome"]["outcome"] == "applied"


async def test_an_outcome_must_be_one_the_contract_names(
    sockets: Sockets,
    raised: Callable[..., Awaitable[dict[str, Any]]],
    recorded: Callable[..., dict[str, Any]],
) -> None:
    """
    GIVEN: A decision
     WHEN: the broker records an outcome that isn't applied, conflict or failed, or
           one for a decision that doesn't exist
     THEN: both are refused
    """
    decision = recorded(await raised())

    unnamed = await sockets.broker.refused(
        "record_outcome", decision=decision["id"], outcome="done"
    )
    missing = await sockets.broker.refused(
        "record_outcome", decision="nope", outcome="applied"
    )

    with check:
        assert unnamed == "invalid_argument"
    with check:
        assert missing == "not_found"


async def test_a_grant_covers_only_its_own_run_session_and_time(
    store: ControlStore,
    clock: FakeClock,
    requester: dict[str, str],
    mount_granted: Callable[..., str],
    covers: Callable[..., Awaitable[str | None]],
    faker: Faker,
) -> None:
    """
    GIVEN: Grants to mount a repository, one for a run, one for a session, one until
           a time, and one revoked
     WHEN: the broker asks whether a request is covered
     THEN: the run's grant covers only that run, the session's only that session, the
           timed one only until its time, and the revoked one never
    """
    run, session = requester["run"], requester["session"]

    # A grant held to a run covers that run, and no other.
    #
    for_the_run = mount_granted("run")
    with check:
        assert await covers(run=run) == for_the_run
    with check:
        assert await covers(run=faker.uuid4()) is None

    # Once it is revoked, a grant held to the session covers that session and no
    # other, and the run is covered by nothing.
    #
    store.revoke_grants([for_the_run], "test")
    for_the_session = mount_granted("session")
    with check:
        assert await covers(session=session) == for_the_session
    with check:
        assert await covers(session=faker.sha256()) is None
    with check:
        assert await covers(run=run) is None

    # A grant until 09:00 UTC, an hour after the clock, covers any run until then,
    # and nothing an hour later.
    #
    store.revoke_grants([for_the_session], "test")
    until_nine = mount_granted("until", until="2027-01-15T09:00:00Z")
    with check:
        assert await covers() == until_nine
    clock.now += 2 * 60 * 60
    with check:
        assert await covers() is None


async def test_a_grant_on_a_folder_isnt_a_grant_on_a_secret_in_it(
    mount_granted: Callable[..., str],
    covers: Callable[..., Awaitable[str | None]],
) -> None:
    """
    GIVEN: A grant to mount a repository, kept until revoked
     WHEN: the broker asks whether it covers a folder in the repository, and whether
           it covers a `.env` file there, asking for that file exactly, as it does
           for a file named like a secret
     THEN: the folder is covered, and the file isn't: a mount of the repository
           leaves the file out, so the grant never let a script read it
      AND: a grant on the file itself covers it
    """
    folder = mount_granted("permanent", "/h/src/api")
    with check:
        assert await covers("/h/src/api/rules") == folder
    with check:
        assert await covers("/h/src/api/.env") == folder
    with check:
        assert await covers("/h/src/api/.env", exact=True) is None

    file = mount_granted("permanent", "/h/src/api/.env")
    with check:
        assert await covers("/h/src/api/.env", exact=True) == file


async def test_the_broker_reads_which_parts_of_its_item_were_refused(
    store: ControlStore,
    sockets: Sockets,
    raised: Callable[..., Awaitable[dict[str, Any]]],
    recorded: Callable[..., dict[str, Any]],
) -> None:
    """
    GIVEN: A request the broker raised to mount two paths
     WHEN: you approve one and refuse the other, and the broker asks what became of it
     THEN: it reads that the item was decided, and each part's outcome, so a rerun
           of the same request isn't put to you again
      AND: once you let the requester ask again, it reads when, so the next rerun
           is put to you
      AND: an item that doesn't exist is `not_found`
    """
    item = await raised(MountsFactory, repositories=("api", "notes"))
    parts = {"api": "approve", "notes": "deny"}

    # Until you decide, the broker reads only that the item waits.
    #
    waiting = await sockets.broker.call("status_of", item=item["id"])
    assert waiting == {"item": item["id"], "state": "waiting"}

    # Once you approve one mount and refuse the other, it reads what you chose for
    # each part.
    #
    recorded(item, parts=parts, remember=[{"pattern": "mount-api", "lasts": "session"}])
    decided = await sockets.broker.call("status_of", item=item["id"])
    with check:
        assert decided["state"] == "decided"
    with check:
        assert decided["decision"]["action"] == "approve"
    with check:
        assert decided["decision"]["parts"] == parts
    with check:
        assert "ask_again" not in decided

    # Letting the requester ask again shows in the status, with when you did.
    #
    lifted = store.let_ask_again(item["id"], "passkey:k")
    after = await sockets.broker.call("status_of", item=item["id"])
    with check:
        assert after["ask_again"] == lifted["ask_again"]["at"]
    with check:
        assert await sockets.broker.refused("status_of", item="nope") == "not_found"


async def test_an_item_past_its_expiry_goes_back_to_its_requester(
    store: ControlStore,
    clock: FakeClock,
    sockets: Sockets,
    raised: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    """
    GIVEN: A waiting item that expires in an hour, given in another time zone
     WHEN: the control plane sweeps half an hour on, and an hour and a half on
     THEN: it still waits after the first sweep, and is expired after the second,
           which the requester sees in its status
    """
    # 10:00 at +01:00 is 09:00 UTC, an hour after the clock.
    #
    item = await raised(expires_at="2027-01-15T10:00:00+01:00")

    clock.now += 30 * 60
    assert store.expire_due() == []
    clock.now += 60 * 60
    assert store.expire_due() == [item["id"]]

    status = await sockets.gateway.call(
        "item_status", item=item["id"], principal=PRINCIPAL
    )
    assert status["state"] == "expired"


###############################################################################
# The gateway's socket


async def test_an_agents_question_keeps_its_words_as_its_claim(
    sockets: Sockets, requester: dict[str, str], faker: Faker
) -> None:
    """
    GIVEN: An agent with a question for you, with choices and a reason
     WHEN: the gateway submits it
     THEN: the item is a question whose title is the question, with its whitespace
           run together, so the list shows what is asked; whose claim is the agent's
           reason and whose choices are the agent's; whose requester and blocking say
           who asked and that they're waiting, with no facts repeating them; and which
           offers no pattern to remember
    """
    why = faker.sentence()
    choices = faker.words(2, unique=True)

    item = await sockets.gateway.call(
        "submit_question",
        principal=PRINCIPAL,
        session=requester["session"],
        question="Which repository holds\n  the billing work?",
        why=why,
        choices=choices,
        blocking=True,
    )

    # Each is a separate thing the item says, so each is reported on its own.
    #
    with check:
        assert item["kind"] == "question"
    with check:
        assert item["title"] == "Which repository holds the billing work?"
    with check:
        assert item["claim"] == why
    with check:
        assert [c["label"] for c in item["choices"]] == choices
    with check:
        assert item["requester"] == {
            "principal": PRINCIPAL,
            "session": requester["session"],
        }
    with check:
        assert item["facts"] == []
    with check:
        assert item["blocking"] is True
    with check:
        assert "patterns" not in item


@pytest.mark.parametrize(
    "params",
    [
        {"facts": [{"label": "Approved by", "value": "you"}]},
        {"patterns": [{"id": "p", "access": "mount", "resource": "~"}]},
        {"kind": "grant"},
        {"choices": ["x"] * 11},
        {"choices": ["x" * 201]},
        {"question": "x" * 301},
        {"why": "x" * 2001},
        {"question": "   "},
        {"risk": "low"},
    ],
    ids=[
        "facts",
        "patterns",
        "another kind",
        "too many choices",
        "a long choice",
        "a long question",
        "a long reason",
        "a blank question",
        "its own risk",
    ],
)
async def test_an_agent_cant_dress_its_words_as_facts(
    store: ControlStore, sockets: Sockets, params: dict[str, Any]
) -> None:
    """
    GIVEN: An agent's question that also tries to set facts, offer a pattern, be
           another kind of item, set its own risk, or is too long or blank
     WHEN: the gateway submits it
     THEN: it is refused, and nothing is queued
    """
    asked = {"principal": PRINCIPAL, "question": "Which one?"}

    code = await sockets.gateway.refused("submit_question", **{**asked, **params})

    assert code == "invalid_argument"
    assert store.list_items("ahead").rows == []


async def test_only_the_requester_sees_or_withdraws_its_item(
    sockets: Sockets, raised: Callable[..., Awaitable[dict[str, Any]]]
) -> None:
    """
    GIVEN: A waiting item a principal asked for
     WHEN: another principal asks for its status or withdraws it, then the requester
           does
     THEN: the other principal is told there is no such item; the requester sees it
           waiting, withdraws it, and sees it withdrawn
    """
    item = await raised()

    for method in ("item_status", "withdraw"):
        code = await sockets.gateway.refused(
            method, item=item["id"], principal="advisor"
        )
        assert code == "not_found"

    status = await sockets.gateway.call(
        "item_status", item=item["id"], principal=PRINCIPAL
    )
    assert status == {"item": item["id"], "state": "waiting"}
    withdrawn = await sockets.gateway.call(
        "withdraw", item=item["id"], principal=PRINCIPAL
    )
    assert withdrawn == {"state": "withdrawn"}
    again = await sockets.gateway.call(
        "item_status", item=item["id"], principal=PRINCIPAL
    )
    assert again["state"] == "withdrawn"


async def test_a_decided_item_cant_be_withdrawn(
    sockets: Sockets,
    raised: Callable[..., Awaitable[dict[str, Any]]],
    recorded: Callable[..., dict[str, Any]],
) -> None:
    """
    GIVEN: An item already decided
     WHEN: its requester withdraws it
     THEN: nothing changes, and the answer says it was decided
    """
    item = await raised()
    recorded(item)

    withdrawn = await sockets.gateway.call(
        "withdraw", item=item["id"], principal=PRINCIPAL
    )

    assert withdrawn == {"state": "decided"}
