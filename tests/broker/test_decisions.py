"""
Tests for the broker's side of the decision feed: your decisions, each applied once.

The feed reads a real control plane, through the `skills` fixture, whose promotion
items are what you decide.
"""

# 3rd party imports
import pytest

# project imports
from maji_mcp.broker.decisions import DecisionFeed
from tests.support.skills import Skills


@pytest.mark.usefixtures("promoted")
async def test_decisions_are_applied_once_across_a_restart(skills: Skills) -> None:
    """
    GIVEN: A promotion you decided, which the broker applied
     WHEN: a restarted broker reads the decision feed from the position it kept
     THEN: it doesn't apply the decision again, and only one outcome is recorded
    """
    # A broker that restarts makes a new feed over the position file the last one
    # kept, with the same appliers.
    #
    restarted = DecisionFeed(
        skills.feed.control_plane, skills.feed.position_file, skills.feed.appliers
    )

    assert await restarted.catch_up() == 0
    outcomes = skills.store.db.execute(
        "SELECT COUNT(*) FROM decision_outcomes"
    ).fetchone()
    assert outcomes[0] == 1
