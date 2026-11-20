"""
Fixtures for the control plane's tests: its database on a clock the test moves, its
REST API with a client of it, a passkey that is signed in, and functions that put
items, decisions and grants into the database.

What the items are made of is in `factories.py`, and the classes the fixtures hand
out are in `tests/support/control.py`.
"""

# system imports
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
from faker import Faker

# project imports
from maji_mcp.control.passkeys import CODE_ALPHABET
from maji_mcp.control.store import ControlStore
from tests.control.factories import (
    PRINCIPAL,
    ChangesetFactory,
    ItemFactory,
    PatternFactory,
    QuestionFactory,
    ServiceFactory,
    SkillFactory,
)
from tests.support.contract import SPEC
from tests.support.control import INSTANCE, Control, Touches, choice, serving
from tests.support.services import FakeClock


@pytest.fixture(autouse=True)
def restarted_sequence() -> None:
    """
    The factories' sequence, started afresh for each test, so the version a
    `SkillFactory` gives a skill repeats from run to run. The root `conftest.py` does
    the same for the values the factories make up.
    """
    # Every `DictFactory` counts on one sequence, which only its base class resets
    # unless forced.
    #
    SkillFactory.reset_sequence(force=True)


@pytest.fixture
def clock(clock: FakeClock) -> FakeClock:
    """
    The shared clock, started at 08:00 UTC on 2027-01-15: the times these tests give
    items and grants are written against that morning.
    """
    clock.now = 1_800_000_000.0
    return clock


@pytest.fixture
def store(clock: FakeClock) -> Iterator[ControlStore]:
    """The database of the instance `studio-mac`, in memory, on the test's clock."""
    store = ControlStore(":memory:", INSTANCE, clock)
    yield store
    store.db.close()


@pytest.fixture
async def control(store: ControlStore, faker: Faker) -> AsyncIterator[Control]:
    """
    The REST API over the database, as a client of it: a browser with no session
    yet, holding a client token and the setup code of an instance with no passkey.
    """
    token = faker.sha256()
    setup_code = faker.lexify("?" * 16, letters=CODE_ALPHABET)
    store.add_client_token("cli", token)
    store.add_setup_code(setup_code)
    async with serving(store, token=token, setup_code=setup_code) as control:
        yield control


@pytest.fixture
async def passkey(control: Control) -> Touches:
    """
    The instance's first passkey, registered with the setup code, with the browser
    signed in by it.
    """
    key = Touches()
    registered = await control.register(key, setup_code=control.setup_code)
    assert registered.status_code == 201
    assert (await control.sign_in(key)).status_code == 204
    return key


@pytest.fixture
def queued(store: ControlStore) -> Callable[..., dict[str, Any]]:
    """
    A function that puts an item in the queue and returns it: a changeset, or what
    the factory given builds, with any changes to what the factory would make.
    """

    def queued(
        of: type[ItemFactory] = ChangesetFactory, **changes: Any
    ) -> dict[str, Any]:
        return store.create_item(**of.build(**changes))

    return queued


@pytest.fixture
def recorded(store: ControlStore) -> Callable[..., dict[str, Any]]:
    """
    A function that records your decision on an item straight into the database, as
    the REST API does once the proof holds, and returns the decision.
    """

    def recorded(
        item: dict[str, Any], action: str = "approve", **extra: Any
    ) -> dict[str, Any]:
        [decision] = store.record_decisions(
            [choice(item, action, **extra)], {"kind": "you", "passkey": "k"}
        )
        return decision

    return recorded


@pytest.fixture
def granted(store: ControlStore) -> Callable[..., dict[str, Any]]:
    """
    A function that records a grant straight into the database and returns it: to
    mount a repository, kept until revoked, unless told otherwise.
    """

    def granted(
        lasts: str = "permanent",
        *,
        principal: str = PRINCIPAL,
        session: str | None = None,
        **pattern: Any,
    ) -> dict[str, Any]:
        return store.add_grant(
            principal, PatternFactory.build(**pattern), lasts, session=session
        )

    return granted


@pytest.fixture
def service(store: ControlStore) -> dict[str, Any]:
    """
    A service the broker can call with a key, reported to the control plane with no
    connection yet: the tracker of `ServiceFactory`.
    """
    definition: dict[str, Any] = ServiceFactory.build()
    store.report_services([{**definition, "connection": None}])
    return definition


@pytest.fixture
def state_of(store: ControlStore) -> Callable[[dict[str, Any]], str]:
    """A function that reads the state an item is in now."""

    def state_of(item: dict[str, Any]) -> str:
        return str(store.status_of(item["id"])["state"])

    return state_of


@pytest.fixture
def database(tmp_path: Path) -> Callable[..., tuple[Path, str]]:
    """
    A function that makes an instance's database file with one question waiting in
    it, and returns the file and the question's id.
    """

    def database(
        instance: str = INSTANCE, name: str = "control.db"
    ) -> tuple[Path, str]:
        path = tmp_path / name
        store = ControlStore(path, instance)
        item = store.create_item(**QuestionFactory.build())
        store.db.close()
        return path, str(item["id"])

    return database


@pytest.fixture(scope="session")
def spec() -> dict[str, Any]:
    """The REST contract, `api/openapi.yaml`, loaded once: no test changes it."""
    return SPEC
