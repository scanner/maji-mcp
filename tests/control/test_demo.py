"""
Tests for the demo queue (`demo.py`): seeding it with sample requests, starting it
afresh, and keeping both away from a real instance's database.
"""

# system imports
from collections.abc import Callable, Iterator
from pathlib import Path

# 3rd party imports
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.broker import jira
from maji_mcp.common.protocol import MajiError
from maji_mcp.control import demo
from maji_mcp.control.store import ControlStore
from tests.support.contract import fits
from tests.support.control import INSTANCE, serving


@pytest.fixture
def sample() -> Iterator[ControlStore]:
    """The demo's own database, in memory, with nothing in its queue yet."""
    store = ControlStore(":memory:", demo.DEMO)
    yield store
    store.db.close()


def test_the_demo_seeds_only_its_own_queue(
    store: ControlStore, sample: ControlStore
) -> None:
    """
    GIVEN: A real instance's database, and the demo's
     WHEN: the demo's sample requests are seeded into each, and into the demo twice
     THEN: the real one is refused untouched; the demo gets its samples once, with
           one decided by policy in its history
    """
    with pytest.raises(SystemExit, match="isn't the demo"):
        demo.seed(store)
    assert store.list_items("ahead").rows == []

    made = demo.seed(sample)
    assert len(sample.list_items("ahead").rows) == len(made) - 1
    [decided] = sample.list_items("behind").rows
    assert decided["decision"]["by"]["kind"] == "policy"
    assert demo.seed(sample) == []


@pytest.mark.parametrize("passkeys", [True, False], ids=["keeping passkeys", "all"])
def test_a_demo_reset_starts_its_queue_afresh(
    tmp_path: Path, faker: Faker, passkeys: bool
) -> None:
    """
    GIVEN: The demo's database, with a passkey, its recovery codes, a signed-in
           session, and a seeded queue with a decision in its history
     WHEN: it is reset, keeping its passkeys or not
     THEN: the queue and its history start empty, and are seeded anew
      AND: If the passkeys are kept, the passkey, its unused recovery codes and the
           session carry over, so an open tab stays signed in
      AND: If not, there is no passkey or session, and setup starts again
    """
    # The demo as it stands after some use: a seeded queue, and someone signed in.
    #
    db = tmp_path / "control.db"
    session, code, passkey = faker.sha256(), faker.password(), faker.sha1()
    store = ControlStore(db, demo.DEMO)
    demo.seed(store)
    store.add_passkey(passkey, faker.word(), faker.binary(32), 3)
    store.add_recovery_codes([code])
    store.start_session(session, passkey)
    store.db.close()

    demo.reset(db, passkeys=passkeys)

    # What the reset leaves doesn't depend on one another, so each is reported on
    # its own.
    #
    fresh = ControlStore(db, demo.DEMO)
    with check:
        assert fresh.list_items("ahead").rows == []
    with check:
        assert fresh.list_items("behind").rows == []
    with check:
        assert fresh.passkey_count() == (1 if passkeys else 0)
    with check:
        assert (fresh.session(session) is not None) is passkeys
    with check:
        assert fresh.use_recovery_code(code) is passkeys
    with check:
        assert demo.seed(fresh) != []
    fresh.db.close()


def test_a_demo_reset_refuses_another_instances_database(
    database: Callable[..., tuple[Path, str]],
) -> None:
    """
    GIVEN: A real instance's database where the demo's would be
     WHEN: it is reset as the demo's, passkeys and all
     THEN: it is refused, and keeps its queue
    """
    db, item = database(INSTANCE)

    with pytest.raises(MajiError, match="belongs to instance"):
        demo.reset(db, passkeys=False)

    kept = ControlStore(db, INSTANCE)
    assert [i["id"] for i in kept.list_items("ahead").rows] == [item]
    kept.db.close()


async def test_the_demo_items_fit_the_contract(
    sample: ControlStore, faker: Faker
) -> None:
    """
    GIVEN: The demo queue, seeded
     WHEN: the web client lists both lanes
     THEN: every item fits the contract's schema, so the client sees what a real
           queue would send
    """
    token = faker.sha256()
    sample.add_client_token("cli", token)
    demo.seed(sample)

    async with serving(sample, token=token) as client:
        for lane in ("ahead", "behind"):
            listed = await client.get(
                "/items", params={"lane": lane}, headers=client.bearer()
            )
            assert listed.status_code == 200
            fits(listed, "/items", "get")


def test_the_demos_jira_is_the_one_the_broker_defines() -> None:
    """
    GIVEN: The demo queue's Connect item for Jira, which no broker stands behind
     WHEN: it is compared with the broker's own definition of Jira
     THEN: the two are the same, so the demo shows the form a real Connect item has
    """
    assert demo.DEMO_JIRA == jira.service().definition()
