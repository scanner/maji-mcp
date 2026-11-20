"""
Tests for the session store behind `maji.save` and `maji.load`: the store on its own,
then through the broker and the stub client, as a session's scripts reach it.
"""

# system imports
from collections.abc import Awaitable, Callable
from typing import Any

# 3rd party imports
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.broker.store import IDLE_SECONDS, SessionStore
from maji_mcp.common.protocol import MajiError
from tests.support.services import FakeClock


@pytest.fixture
def store(clock: FakeClock) -> SessionStore:
    """A store with the real limits, whose idle time passes when `clock` is moved."""
    return SessionStore(clock)


def test_a_saved_value_comes_back_as_json_gave_it(
    store: SessionStore, session: str
) -> None:
    """
    GIVEN: A session that saved a value under a name, then saved another under it
     WHEN: a later script loads the name
     THEN: it gets the newer value, as JSON gives it back, and saved() lists the name
           once with its size and time
    """
    store.save(session, "issues", [{"id": "1"}])
    reply = store.save(session, "issues", [{"id": "2", "count": 3}, ("x", 1)])

    with check:
        assert store.load(session, "issues") == [{"id": "2", "count": 3}, ["x", 1]]
    with check:
        assert reply == {
            "name": "issues",
            "bytes": len('[{"id": "2", "count": 3}, ["x", 1]]'),
        }
    [entry] = store.saved(session)
    with check:
        assert entry["name"] == "issues" and entry["bytes"] == reply["bytes"]
    with check:
        assert entry["saved_at"].endswith("+00:00")


@pytest.mark.parametrize(
    ("asking", "name", "told"),
    [("session", "c", ["a", "b"]), ("other_session", "a", [])],
    ids=["a name it never saved", "a name another session saved"],
)
def test_a_name_the_session_hasnt_saved_is_not_found_naming_what_it_has(
    store: SessionStore,
    session: str,
    request: pytest.FixtureRequest,
    asking: str,
    name: str,
    told: list[str],
) -> None:
    """
    GIVEN: A session with values saved under two names, and another with none
     WHEN: the first loads a third name, or the other loads one of the first's
     THEN: each is not_found, with the names its own session has saved, so a script
           can see what it meant
      AND: the other session is told of no names, and its listing is empty: sessions
           never see each other's values
    """
    store.save(session, "b", 1)
    store.save(session, "a", 2)

    # `asking` names the fixture holding the key of the session that loads.
    #
    asker = request.getfixturevalue(asking)

    with pytest.raises(MajiError) as raised:
        store.load(asker, name)
    assert raised.value.code == "not_found"
    assert raised.value.data == {"saved": told}
    assert [entry["name"] for entry in store.saved(asker)] == told


@pytest.mark.parametrize(
    "use",
    [
        lambda store: store.save(None, "x", 1),
        lambda store: store.load(None, "x"),
        lambda store: store.saved(None),
    ],
    ids=["save", "load", "saved"],
)
def test_a_run_without_a_session_keeps_nothing(
    store: SessionStore, use: Callable[[SessionStore], Any]
) -> None:
    """
    GIVEN: A run registered without a session, as from a client that keeps none
     WHEN: its script saves, loads or lists
     THEN: each is unavailable, rather than sharing values with other runs
    """
    with pytest.raises(MajiError) as raised:
        use(store)
    assert raised.value.code == "unavailable"


@pytest.mark.parametrize("name", ["", "a b", "../x", "x" * 65, "é"])
def test_a_name_is_a_short_plain_word(
    store: SessionStore, session: str, name: str
) -> None:
    """
    GIVEN: A name that is empty, too long, or holds anything but letters, digits,
           dots, dashes and underscores
     WHEN: a script saves under it
     THEN: invalid_argument, and nothing is kept
    """
    with pytest.raises(MajiError) as raised:
        store.save(session, name, 1)
    assert raised.value.code == "invalid_argument"
    assert store.saved(session) == []


def test_the_value_and_session_limits_hold(session: str) -> None:
    """
    GIVEN: A store whose limits are a 100-byte value and a 150-byte session
     WHEN: a script saves one value over the value limit, then two that together
           pass the session limit, then replaces the first of those
     THEN: the oversized value and the one past the session limit are refused with
           invalid_argument, and replacing a value counts only its new size
    """
    # Small limits keep the values readable; the real ones are megabytes.
    #
    store = SessionStore(max_value=100, max_session=150)

    with pytest.raises(MajiError) as too_big:
        store.save(session, "big", "x" * 200)
    assert too_big.value.code == "invalid_argument"
    store.save(session, "one", "x" * 90)
    with pytest.raises(MajiError) as too_many:
        store.save(session, "two", "x" * 90)
    assert too_many.value.code == "invalid_argument"
    store.save(session, "one", "x" * 98)
    assert [e["name"] for e in store.saved(session)] == ["one"]


def test_past_the_total_the_least_recently_used_session_goes(
    clock: FakeClock, faker: Faker
) -> None:
    """
    GIVEN: A store whose sessions together may hold 250 bytes, and two sessions
           holding about 100 each, the first used less recently
     WHEN: a third session saves 100 bytes
     THEN: the least recently used session is dropped, and the other two keep their
           values
    """
    store = SessionStore(clock, max_value=200, max_session=200, max_total=250)
    oldest, newer, newest = (faker.sha256() for _ in range(3))
    store.save(oldest, "v", "x" * 98)
    clock.now += 1
    store.save(newer, "v", "x" * 98)
    clock.now += 1
    store.save(newest, "v", "x" * 98)

    assert store.saved(oldest) == []
    assert store.load(newer, "v") == "x" * 98
    assert store.load(newest, "v") == "x" * 98


def test_a_session_unused_for_an_hour_is_dropped(
    store: SessionStore, clock: FakeClock, session: str
) -> None:
    """
    GIVEN: A session that saved a value
     WHEN: it is used again just under an hour later, then left for over an hour
     THEN: the value survives the first gap, because each use keeps the session
           alive, and is gone after the second
    """
    store.save(session, "v", 1)
    clock.now += IDLE_SECONDS - 1
    assert store.load(session, "v") == 1
    clock.now += IDLE_SECONDS + 1
    with pytest.raises(MajiError) as raised:
        store.load(session, "v")
    assert raised.value.code == "not_found"


###############################################################################
#
# Through the broker: a run's values belong to the session it was registered under.


async def test_a_later_script_in_the_session_loads_what_an_earlier_saved(
    run_script: Callable[..., Awaitable[Any]],
    script_error: Callable[..., Awaitable[Any]],
    session: str,
    other_session: str,
) -> None:
    """
    GIVEN: A script that saved a result, in a run registered under a session
     WHEN: a later run in the same session loads it, and a run in another session
           tries to
     THEN: the later script gets the value; the other session's gets not_found
    """
    issues = [{"id": "1", "count": 5}, {"id": "2", "count": 7}]
    saved = await run_script(
        lambda maji: maji.save("issues", issues), "keeper", session
    )
    assert saved["name"] == "issues"

    later = await run_script(lambda maji: maji.load("issues"), "keeper", session)
    assert later == issues

    elsewhere = await script_error(
        lambda maji: maji.load("issues"), "keeper", other_session
    )
    assert elsewhere.code == "not_found"


async def test_a_run_without_a_session_cannot_save(
    script_error: Callable[..., Awaitable[Any]],
) -> None:
    """
    GIVEN: A run registered with no session
     WHEN: its script saves a value
     THEN: the call fails with unavailable, rather than sharing a store with other
           runs
    """
    error = await script_error(lambda maji: maji.save("x", 1), "keeper")
    assert error.code == "unavailable"
