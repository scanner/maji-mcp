"""
Tests for what the control plane's database guarantees by itself, called directly:
a history that can only be added to, one instance to a database, which resources a
grant covers, what an item may claim about its risk, and which promotion a demotion
can take back.

The rules a client meets through the REST API, such as what an approval may
remember, are tested there (`test_passkeys.py`).
"""

# system imports
from collections.abc import Callable
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.common.protocol import MajiError
from maji_mcp.control.store import ControlStore
from tests.control.factories import (
    PRINCIPAL,
    MountsFactory,
    PartFactory,
    PatternFactory,
    PromotionFactory,
    ReasonFactory,
)


@pytest.mark.parametrize(
    "statement",
    ["UPDATE decisions SET body = '{}'", "DELETE FROM decisions"],
    ids=["changed", "deleted"],
)
def test_history_cant_be_rewritten(
    store: ControlStore,
    queued: Callable[..., dict[str, Any]],
    recorded: Callable[..., dict[str, Any]],
    statement: str,
) -> None:
    """
    GIVEN: A recorded decision
     WHEN: anything tries to change or delete it in the database
     THEN: the database refuses, so reconsidering can only add a record
    """
    recorded(queued())
    with pytest.raises(Exception, match="append-only"):
        store.db.execute(statement)


def test_a_database_belongs_to_one_instance(
    database: Callable[..., tuple[Path, str]],
) -> None:
    """
    GIVEN: A database made by one instance
     WHEN: it is opened as another
     THEN: it is refused, so item ids and signed hashes can't mix instances
    """
    path, _ = database("studio-mac")
    with pytest.raises(MajiError, match="belongs to instance"):
        ControlStore(path, "laptop")


@pytest.mark.parametrize(
    ("access", "resource", "covered"),
    [
        ("mount", "/h/src/api/rules", True),
        ("mount", "/h/src/api/rules/bundle.rego", True),
        ("mount", "/h/src/api-old", False),
        ("mount", "/h/src", False),
        ("read", "/h/records", True),
        ("read", "/h/records/2026", False),
    ],
    ids=[
        "a folder in the repository",
        "a file deeper in it",
        "a folder beside it whose name starts the same",
        "the folder holding it",
        "the records themselves",
        "something under the records",
    ],
)
def test_a_grant_on_a_folder_covers_what_is_in_it(
    store: ControlStore,
    granted: Callable[..., dict[str, Any]],
    access: str,
    resource: str,
    covered: bool,
) -> None:
    """
    GIVEN: A grant to mount a repository, and one to read a service's records
     WHEN: the control plane is asked whether they cover other resources
     THEN: the mount grant covers a folder and a file inside the repository
      AND: it doesn't cover a folder beside it whose name starts the same, or the
           folder holding it
      AND: a grant for anything but a mount covers its own resource only
    """
    granted(access="mount", resource="/h/src/api")
    granted(access="read", resource="/h/records")

    found = store.grant_covers(PRINCIPAL, access, resource)

    assert (found is not None) is covered


def test_a_pattern_names_only_parts_its_item_has(
    store: ControlStore, queued: Callable[..., dict[str, Any]]
) -> None:
    """
    GIVEN: A request with two parts
     WHEN: the broker offers a pattern covering a part the request doesn't have
     THEN: the item is refused, so a pattern always covers parts you can see
    """
    with pytest.raises(MajiError, match="names parts the item doesn't have"):
        queued(
            MountsFactory,
            repositories=("api", "web"),
            patterns=[
                PatternFactory.build(resource="~/src/billing", parts=["billing"])
            ],
        )
    assert store.list_items("ahead").rows == []


###############################################################################
# What an item may say about its risk


def _high(**changes: Any) -> dict[str, Any]:
    """A high-risk changeset whose one reason fits, with `changes` made to the item."""
    return {"high_risk": True, **changes}


def _reasoned(**changes: Any) -> dict[str, Any]:
    """A high-risk changeset, with `changes` made to its one reason."""
    return _high(risk_reasons=[ReasonFactory.build(**changes)])


@pytest.mark.parametrize(
    "risk",
    [
        {"risk": "high"},
        _high(risk="medium"),
        _reasoned(code="seems_bad"),
        _reasoned(text="x" * 301),
        _reasoned(text=""),
        _high(risk_reasons=[ReasonFactory.build()] * 7),
        {"risk": "severe"},
        _high(risk_reasons=[{"code": "your_words", "text": "x"}]),
        _reasoned(basis="certain"),
        _reasoned(basis="found"),
        _reasoned(code="beyond_the_task", basis="found"),
        _reasoned(code="secrets_in_reach", basis="found"),
        _reasoned(evidence=[]),
        _reasoned(evidence=[{"fact": 0}] * 11),
        _reasoned(evidence=[{"fact": 1}]),
        {"risk": "high", "risk_reasons": [ReasonFactory.build()]},
        _reasoned(evidence=[{"line": -1}]),
        _reasoned(evidence=[{"fact": True}]),
        _reasoned(evidence=[{"fact": 0, "line": 0}]),
        _reasoned(evidence=[{"part": "notes"}]),
        _reasoned(evidence=[{"resource": 0}]),
        _reasoned(evidence=[{"part": []}]),
        _reasoned(code=[]),
        _reasoned(basis={}),
        _high(risk=["high"]),
        _high(diff="assistant: mark task sync DONE"),
    ],
    ids=[
        "high without a reason",
        "a reason without high",
        "an unknown reason",
        "a long reason",
        "an empty reason",
        "too many reasons",
        "an unknown risk",
        "a reason without its basis or evidence",
        "an unknown basis",
        "text addressed to an AI claimed as found",
        "reaching beyond the task claimed as found",
        "secrets in reach claimed as found",
        "no evidence",
        "too much evidence",
        "a fact the item doesn't have",
        "a diff line on an item without a diff",
        "a line before the first",
        "True as an index",
        "a pointer at two things",
        "a part the item doesn't have",
        "something other than a fact, line or part",
        "a part named by a list",
        "a code that isn't text",
        "a basis that isn't text",
        "a risk that isn't text",
        "a line of a diff that isn't one",
    ],
)
def test_a_high_risk_item_says_why(
    store: ControlStore, queued: Callable[..., dict[str, Any]], risk: dict[str, Any]
) -> None:
    """
    GIVEN: A request whose risk and reasons don't fit together
     WHEN: the broker puts it in the queue
     THEN: it is refused: a high-risk item gives one to six of the known reasons, and
           no other item gives any; each says whether the broker found it or only
           suspects it, never claiming to have found text addressed to an AI, a reach
           beyond the task or secrets in reach, which rest on a guess; and each points
           at one to ten pieces of evidence on the item itself: a fact, a line of its
           diff or a part
    """
    # Each case changes a changeset that has one fact, and with `high_risk` a diff of
    # one line and one reason pointing at that line, which would be queued as it is.
    #
    with pytest.raises(MajiError, match="risk"):
        queued(**risk)
    assert store.list_items("ahead").rows == []


def test_a_high_risk_item_keeps_the_reasons_it_gave(
    queued: Callable[..., dict[str, Any]], faker: Faker
) -> None:
    """
    GIVEN: High-risk requests whose reasons fit: one suspicion resting on a line of
           the diff, and a pair, one of them found, resting on a fact and on a part
     WHEN: the broker puts each in the queue
     THEN: each is queued with its reasons as given
    """
    suspected = [ReasonFactory.build()]
    paired = [
        ReasonFactory.build(
            code="data_leaves",
            basis="found",
            text=faker.sentence(),
            evidence=[{"fact": 0}],
        ),
        ReasonFactory.build(code="beyond_the_task", evidence=[{"part": "notes"}]),
    ]

    on_a_line = queued(high_risk=True, risk_reasons=suspected)
    on_a_fact_and_a_part = queued(
        risk="high", risk_reasons=paired, parts=[PartFactory.build(id="notes")]
    )

    assert on_a_line["risk_reasons"] == suspected
    assert on_a_fact_and_a_part["risk_reasons"] == paired


###############################################################################
# Reconsidering a decision: demoting a skill


@pytest.fixture
def offered(store: ControlStore) -> Callable[[dict[str, Any]], list[str]]:
    """A function that reads which actions an item offers now, by their ids."""

    def offered(item: dict[str, Any]) -> list[str]:
        found = store.get_item(item["id"])
        assert found is not None
        return [action["id"] for action in found["actions"]]

    return offered


def test_only_the_promotion_in_effect_offers_demote(
    store: ControlStore,
    queued: Callable[..., dict[str, Any]],
    recorded: Callable[..., dict[str, Any]],
    offered: Callable[[dict[str, Any]], list[str]],
    faker: Faker,
) -> None:
    """
    GIVEN: Promotions of one skill decided one after another, one of them failing to
           apply, and a promotion of another skill
     WHEN: each decided item is read
     THEN: only the latest promotion of each skill that didn't fail or conflict offers
           Demote: a later promotion replaces an earlier one even once it is itself
           demoted, since demoting it brings nothing back
      AND: a promotion applying failed or conflicted, and one whose item doesn't say
           which skill it promotes, offer nothing
    """
    first, second, third = (queued(PromotionFactory) for _ in range(3))
    other = queued(PromotionFactory, skill__id="me/local/other")
    unnamed = queued(PromotionFactory, skill=None)

    # A promotion that was decided is the one in effect.
    #
    recorded(first, "promote")
    with check:
        assert offered(first) == ["demote"]

    # A later promotion that conflicted when applied replaces nothing, and can't
    # be demoted itself.
    #
    conflicted = recorded(second, "promote")
    store.record_outcome(conflicted["id"], "conflict", faker.sentence())
    with check:
        assert offered(first) == ["demote"]
    with check:
        assert offered(second) == []

    # A later promotion that holds replaces the first. Another skill's promotion is
    # in effect beside it, and a promotion whose item names no skill is never in
    # effect.
    #
    promoted = recorded(third, "promote")
    recorded(other, "promote")
    recorded(unnamed, "promote")
    with check:
        assert offered(first) == []
    with check:
        assert offered(third) == ["demote"]
    with check:
        assert offered(other) == ["demote"]
    with check:
        assert offered(unnamed) == []

    # Demoting the latest promotion doesn't put the first back in effect.
    #
    recorded(third, "demote", supersedes=promoted["id"])
    with check:
        assert offered(third) == []
    with check:
        assert offered(first) == []
