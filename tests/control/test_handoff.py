"""
Tests for the keys held for the broker on their way to its credential store
(`src/maji_mcp/control/handoff.py`).
"""

# 3rd party imports
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.control.handoff import HOLD_SECONDS, KeyHandoff
from tests.support.services import FakeClock


def test_a_key_is_taken_once_and_forgotten_after_its_time(
    clock: FakeClock, faker: Faker
) -> None:
    """
    GIVEN: Two keys held for the broker, each by its decision
     WHEN: the broker takes the first twice, and the second only after it has waited
           longer than HOLD_SECONDS
     THEN: the first is given once, then never again
      AND: the second is gone, so a key never waits in memory for long
      AND: what the holder shows of itself is how many it holds, never a key
    """
    keys = KeyHandoff(clock)
    first, second = faker.sha256(), faker.sha256()
    keys.hold("d-1", first)
    keys.hold("d-2", second)

    with check:
        assert first not in repr(keys) and second not in repr(keys)
    with check:
        assert (keys.take("d-1"), keys.take("d-1")) == (first, None)

    clock.now += HOLD_SECONDS + 1
    with check:
        assert keys.take("d-2") is None
    with check:
        assert len(keys) == 0
