"""
Tests for skills as an agent reaches them (`src/maji_mcp/gateway/server.py`): the
gateway's `save_skill` and `promote_skill` tools, the scripts the gateway keeps for
`save_skill`, and what each tool's result says.

The first tests drive the gateway over HTTP as an MCP client, in front of a broker
with a skill library and a control plane. The gateway's other tests are in
`test_server.py`.
"""

# system imports
from collections.abc import Awaitable, Callable
from typing import Any

# 3rd party imports
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.common.names import session_name
from maji_mcp.gateway.server import RecentScripts, format_promoted, format_saved
from tests.support.gateway import KEEPER_TOKEN, Connect
from tests.support.skills import TRIED, Skills


@pytest.fixture
def in_a_session(connected: Connect) -> Callable[..., Awaitable[list[str]]]:
    """
    A function that, in one MCP session as the keeper principal, runs TRIED with
    run_python and then calls each tool it is given, in order, with "RUN" in an
    argument standing for the id TRIED's result ended with. It returns TRIED's
    result, then each call's text, or for a failed call its error text.
    """

    async def in_a_session(*calls: tuple[str, dict[str, Any]]) -> list[str]:
        async with connected(KEEPER_TOKEN) as session:
            ran = await session.call_tool("run_python", {"code": TRIED})
            text: str = ran.content[0].text  # type: ignore[union-attr]
            run = text.rsplit("[run ", 1)[1][:8]
            texts = [text]
            for tool, args in calls:
                named = {k: run if v == "RUN" else v for k, v in args.items()}
                result = await session.call_tool(tool, named)
                texts.append(result.content[0].text)  # type: ignore[union-attr]
        return texts

    return in_a_session


async def test_an_agent_keeps_a_function_by_naming_the_run_it_tried_it_in(
    skills: Skills, in_a_session: Callable[..., Awaitable[list[str]]], faker: Faker
) -> None:
    """
    GIVEN: An agent that ran a script defining a function, which succeeded
     WHEN: it keeps the function with save_skill, naming the run by the id its
           result ended with, calls it in a later script, lists and reads its API,
           and asks for it to be promoted
     THEN: the run's result names what it called; keeping it says what was kept and
           left out, what it reads with, and that it's the agent's to call now
      AND: the later script calls it, its result naming the skill and the stub it
           called; list_api and read_api show it as kept and not reviewed
      AND: asking to promote it names the item in your queue, by the session's name
           as you'll see it, and says it is ordinary because it only reads
    """
    ran, kept, called, listed, read, asked = await in_a_session(
        ("save_skill", {"name": "keys", "summary": "Sorted keys.", "run": "RUN"}),
        ("run_python", {"code": "print(skills.keys())"}),
        ("list_api", {}),
        ("read_api", {"name": "skills.keys"}),
        ("promote_skill", {"name": "keys", "why": faker.sentence()}),
    )

    # The run names what it called, and keeping its function says what was kept.
    #
    first, left_out, reach, yours = kept.split("\n")
    with check:
        assert ran.endswith(", 3 ms; called fake.list]")
    with check:
        assert first.startswith("Kept skills.keys, version ")
    with check:
        assert first.endswith(": skills.keys(limit: int = 2) -> list")
    with check:
        assert left_out == "Left out of the script: lines 7."
    with check:
        assert reach == "Reads with fake.list."
    with check:
        assert yours == (
            "It's yours to call now, in this session and later ones. Other agents "
            "get it only if the user promotes it."
        )

    # A later script calls it, and the agent's API shows it as kept, not reviewed.
    #
    with check:
        assert called.startswith("['limit', 'tags']\n[run ")
    with check:
        assert called.endswith(", 3 ms; called skills.keys, fake.list]")
    with check:
        assert "  skills.keys: Sorted keys. [kept by you, not reviewed]" in listed
    with check:
        assert "Kept by you, not reviewed: me/local/keys, version " in read

    # Asking to promote it names the item that now waits in your queue.
    #
    [item] = skills.store.list_items("ahead").rows
    named = session_name(item["requester"]["session"])
    assert asked.split("\n") == [
        f"Asked: skills.keys, version {first.split()[3][:-1]} waits on {item['id']} "
        f"in the user's queue, which shows this session as {named}.",
        "It only reads, so it is an ordinary item for the user.",
    ]


async def test_a_run_save_skill_cant_find_is_refused_saying_what_to_do(
    skills: Skills, in_a_session: Callable[..., Awaitable[list[str]]], faker: Faker
) -> None:
    """
    GIVEN: An agent in a session with one successful run
     WHEN: it names a run this session doesn't have, a start of an id too short to
           be sure of, or both a run and code
     THEN: each is refused, saying to run it again or send the code, and nothing is
           kept
    """
    keys = {"name": "keys", "summary": faker.sentence()}
    _, unknown, short, both = await in_a_session(
        ("save_skill", {**keys, "run": "0123abcd"}),
        ("save_skill", {**keys, "run": "a"}),
        ("save_skill", {**keys, "run": "RUN", "code": TRIED}),
    )

    with check:
        assert (
            "no run 0123abcd of this session that succeeded; run it again, or send"
            in unknown
        )
    with check:
        assert "no run a of this session" in short
    with check:
        assert "name the run, or send the code; not both" in both
    with check:
        assert skills.library.names() == []


def test_a_session_keeps_its_latest_successful_scripts_within_bounds() -> None:
    """
    GIVEN: The scripts kept for save_skill, bounded per session and in all
     WHEN: more scripts run than either bound allows
     THEN: each session keeps its latest; past the total, the session used longest
           ago gives up its oldest scripts until the rest fit; and a run is found by
           its id or by a start of it that only it has, at least 8 characters long
    """
    # A session keeps its latest two, found by eight characters or more of an id,
    # and by that session alone.
    #
    kept = RecentScripts(per_session=2, max_bytes=30)
    kept.keep("s1", "aaaa1111aaaa", "one")
    kept.keep("s1", "aaaa2222bbbb", "two")
    kept.keep("s1", "cccc3333cccc", "three")
    assert kept.find("s1", "aaaa1111") is None
    assert kept.find("s1", "aaaa2222") == ("aaaa2222bbbb", "two")
    assert kept.find("s1", "cccc333") is None
    assert kept.find("s2", "cccc3333") is None

    # Past the total, the session used longest ago gives up its oldest script.
    #
    kept.keep("s2", "dddd4444dddd", "x" * 25)
    assert kept.find("s1", "aaaa2222") is None
    assert kept.find("s1", "cccc3333") == ("cccc3333cccc", "three")
    assert kept.find("s2", "dddd4444") == ("dddd4444dddd", "x" * 25)

    # A start that two runs share finds neither.
    #
    ambiguous = RecentScripts()
    ambiguous.keep("s", "eeee5555aaaa", "a")
    ambiguous.keep("s", "eeee5555bbbb", "b")
    assert ambiguous.find("s", "eeee5555") is None
    assert ambiguous.find("s", "eeee5555b") == ("eeee5555bbbb", "b")


def test_keeping_tells_the_agent_what_it_reaches_and_what_still_waits() -> None:
    """
    GIVEN: A skill kept that writes, can't all be followed from its code, has an
           earlier version waiting to be promoted, and a note you left on one
     WHEN: the result is described to the agent
     THEN: it says what it writes with, what can't be followed, that it's the
           agent's to call now, which item still waits, and your note
      AND: kept as the version already promoted, it says every agent has it
    """
    kept = {
        "name": "bump",
        "version": "0123456789abcdef",
        "signature": "skills.bump(key: str) -> dict",
        "dropped": [],
        "reads": [],
        "writes": ["org.update_task"],
        "dynamic": ["line 2: eval()"],
        "state": "kept",
        "waiting": "i-1",
        "note": "Say what it returns.",
    }
    assert format_saved(kept).split("\n") == [
        "Kept skills.bump, version 0123456789abcdef: skills.bump(key: str) -> dict",
        "Reads with no stubs; writes with org.update_task.",
        "Can't be followed from its code: line 2: eval().",
        "It's yours to call now, in this session and later ones. Other agents get it "
        "only if the user promotes it.",
        "Your request to promote an earlier version still waits on i-1; ask again "
        "with promote_skill to put this one in its place.",
        "The user's note on an earlier version: Say what it returns.",
    ]
    promoted = format_saved(
        {**kept, "state": "promoted", "waiting": None, "note": None}
    )
    assert "This version is the promoted one, so every agent has it." in promoted


@pytest.mark.parametrize(
    ("asked", "says"),
    [
        (
            {"state": "waiting", "item": "i-1", "risk": "high", "writes": ["org.x"]},
            [
                "Asked: skills.bump, version 0123 waits on i-1 in the user's queue, "
                "which shows this session as a-b-c.",
                "It writes with org.x, so it is high risk, and the user signs it with "
                "their passkey.",
            ],
        ),
        (
            {"state": "waiting", "item": "i-1", "risk": "high", "writes": []},
            [
                "Asked: skills.bump, version 0123 waits on i-1 in the user's queue, "
                "which shows this session as a-b-c.",
                "What it reaches can't all be read from its code, so it is high risk, "
                "and the user signs it with their passkey.",
            ],
        ),
        (
            {"state": "waiting", "item": "i-1", "risk": None, "writes": []},
            [
                "skills.bump, version 0123 already waits on i-1 in the user's queue, "
                "which shows this session as a-b-c."
            ],
        ),
        (
            {"state": "promoted", "item": None, "risk": None, "writes": []},
            ["skills.bump, version 0123 is already promoted, so every agent has it."],
        ),
    ],
    ids=["writes", "unclear", "already asked", "already promoted"],
)
def test_asking_to_promote_tells_the_agent_what_waits_and_why(
    asked: dict[str, Any], says: list[str]
) -> None:
    """
    GIVEN: A request to promote a skill that writes, one whose reach can't all be
           read, one already asked for, and one already promoted
     WHEN: the result is described to the agent
     THEN: it names the item and the session's name, and says why the item is as
           risky as it is, or that nothing more was asked
    """
    text = format_promoted(
        {
            "name": "bump",
            "version": "0123",
            "session_name": "a-b-c",
            "note": None,
            **asked,
        }
    )
    assert text.split("\n") == says
