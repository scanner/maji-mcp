"""
Tests for the control plane's identifiers (`ids.py`): an instance's, made from its
host's name, and an item's, which sorts by when it was made.
"""

# system imports
import re
from typing import Any

# 3rd party imports
import pytest

# project imports
from maji_mcp.control.ids import instance_id, item_id, ulid


@pytest.mark.parametrize(
    ("hostname", "taken", "expected"),
    [
        ("studio-mac.example.net", set(), "studio-mac"),
        ("Studio-Mac", set(), "studio-mac"),
        ("Build_Host.local", set(), "build-host"),
        ("studio-mac", {"studio-mac"}, "studio-mac-2"),
        ("studio-mac", {"studio-mac", "studio-mac-2"}, "studio-mac-3"),
        ("---", set(), "instance"),
    ],
)
def test_an_instance_is_named_after_its_host(
    spec: dict[str, Any], hostname: str, taken: set[str], expected: str
) -> None:
    """
    GIVEN: A host's name, and the instances already on that host
     WHEN: a new instance takes its id
     THEN: it is the name's first label in lower case, with characters a DNS label
           can't hold made hyphens, and `-2` and so on when the name is taken; the
           result always fits the contract's pattern
    """
    made = instance_id(hostname, taken)
    assert made == expected
    pattern = spec["components"]["schemas"]["InstanceIdentifier"]["pattern"]
    assert re.fullmatch(pattern, made)


def test_item_ids_fit_the_contract_and_sort_by_time(spec: dict[str, Any]) -> None:
    """
    GIVEN: An instance making items a millisecond apart
     WHEN: each gets an id
     THEN: every id fits the contract's pattern, and later ids sort after earlier ones
    """
    pattern = spec["components"]["schemas"]["ItemIdentifier"]["pattern"]
    assert re.fullmatch(pattern, item_id("studio-mac-2"))
    ids = [ulid(1_700_000_000_000 + n) for n in range(5)]
    assert ids == sorted(ids)
    assert all(len(i) == 26 for i in ids)
