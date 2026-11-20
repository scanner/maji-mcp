"""
Tests for the broker's credential store (`src/maji_mcp/broker/credentials.py`).
"""

# system imports
import stat
from pathlib import Path

# 3rd party imports
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.broker.credentials import Credential, CredentialStore


def test_a_key_is_kept_by_the_broker_alone_and_replaced_whole(
    tmp_path: Path, faker: Faker
) -> None:
    """
    GIVEN: A credential store in a folder that doesn't exist yet
     WHEN: a key is kept for a service, then replaced by another, then forgotten
     THEN: the folder and the key's file can be read by the broker's user alone
      AND: the second key replaces the first whole, leaving no other file behind
      AND: what the store and a credential show of themselves never holds a key
      AND: once forgotten, the service has no key
    """
    store = CredentialStore(tmp_path / "credentials")
    first, second = (
        Credential(
            key=faker.sha256(),
            settings={"org": "acme"},
            fingerprint="sha256:" + "ab" * 32,
            connected_at="2027-01-15T08:00:00+00:00",
            decision=f"d-{n}",
            item="i-1",
        )
        for n in (1, 2)
    )

    store.put("tracker", first)
    store.put("tracker", second)

    folder = tmp_path / "credentials"
    with check:
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    with check:
        assert [p.name for p in folder.iterdir()] == ["tracker.json"]
    with check:
        assert stat.S_IMODE((folder / "tracker.json").stat().st_mode) == 0o600
    with check:
        assert store.get("tracker") == second
    with check:
        assert second.key not in repr(store) + repr(second)
    with check:
        assert second.key not in str(second.described())

    with check:
        assert (store.delete("tracker"), store.delete("tracker")) == (True, False)
    with check:
        assert store.get("tracker") is None


@pytest.mark.parametrize("name", ["../escape", "Tracker", "", "a" * 33])
def test_a_name_that_isnt_a_services_names_no_file(tmp_path: Path, name: str) -> None:
    """
    GIVEN: A credential store
     WHEN: it is asked for the key of something that isn't a service's name
     THEN: it is refused, so no name reaches a file outside the store's folder
    """
    store = CredentialStore(tmp_path / "credentials")

    with pytest.raises(ValueError):
        store.get(name)
