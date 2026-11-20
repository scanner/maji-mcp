"""
Tests for skills through the broker: keeping a function from a run, calling it from
scripts, asking you to promote it, and applying what you decide.

They drive `broker/skills.py`, and `broker/library.py` and `broker/decisions.py`
behind it, through the broker's control socket, as the gateway does. Everything is
real except Docker: the broker's sockets, the control plane's socket for it and its
store, the decision feed, and the stub client a script uses, which runs scripts in
this process.
"""

# system imports
from typing import Any

# 3rd party imports
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.broker.policy import Grant
from maji_mcp.common.names import session_name
from maji_mcp.common.protocol import MajiError
from maji_mcp.gateway.server import format_saved
from tests.support.skills import SUMMARY, TRIED, Skills

# A script that calls `skills.keys`, and what it prints for TRIED's `keys`.
#
CALLS = "print(skills.keys())"
KEYS = "['limit', 'tags']"

# TRIED with `keys` returning its result backwards, so a script that calls it shows
# which version it was given, and what CALLS then prints; TRIED with only its
# docstring reworded; and TRIED followed by a call that fails.
#
BACKWARDS = TRIED.replace("sorted(", "list(reversed(").replace(
    '["args"])', '["args"]))'
)
KEYS_BACKWARDS = "['tags', 'limit']"
REWORDED = TRIED.replace("first few", "first")
FAILS = TRIED + "\nfake.secret_missing()\n"

# A function that writes with `fake.update`, which the broker classes as a write; one
# that reaches that write through the first; and one that calls a stub by a name it
# works out at run time, with what reading its code says of that.
#
WRITES = "def bump(key: str) -> dict:\n    return fake.update(key=key)\n"
CALLS_A_WRITER = "def bumps(key: str) -> dict:\n    return skills.bump(key=key)\n"
WORKS_OUT_A_NAME = (
    "def pick(kind: str) -> dict:\n    return maji.call(f'fake.{kind}')\n"
)
UNSEEN = "line 2: maji.call with a stub name worked out at run time"
WRITES_WITH = {"label": "Writes with", "value": "fake.update", "flag": True}


###############################################################################
#
# Keeping a skill.


async def test_a_function_kept_from_its_run_is_its_agents_with_nothing_to_approve(
    skills: Skills, other_session: str
) -> None:
    """
    GIVEN: A script that defines a function that reads, calls it and prints what it
           returned, which succeeded
     WHEN: the function is kept as a skill, naming that run
     THEN: the result says what was kept and left out, and what it reads with; your
           queue holds nothing
      AND: a script in another of that agent's sessions calls it as `skills.keys()`,
           its stub calls checked as the script's own, and the load is in the run log
      AND: another agent is told there is no such skill it may run
    """
    run, printed = await skills.run(TRIED)
    assert printed == KEYS
    kept = await skills.keep(run=run)

    # What was kept, what it reaches, and that nothing waits for you.
    #
    with check:
        assert kept["signature"] == "skills.keys(limit: int = 2) -> list"
    with check:
        assert (kept["reads"], kept["writes"]) == (["fake.list"], [])
    with check:
        assert kept["dropped"] == [[7, 7]]
    with check:
        assert (kept["state"], kept["waiting"], kept["note"]) == ("kept", None, None)
    with check:
        assert skills.store.list_items("ahead").rows == []

    # The agent that kept it calls it from another session, and another agent can't.
    #
    later, printed = await skills.run(
        "print(skills.keys(limit=1))", session=other_session
    )
    assert printed == KEYS
    assert skills.services.broker.runlog.calls(later) == [
        ("skills.keys", "ok"),
        ("fake.list", "ok"),
    ]
    _, refused = await skills.run("skills.keys()", principal="helper")
    assert refused.startswith("not_found: no skill 'keys' you may run")


@pytest.mark.parametrize(
    ("ran", "ran_by", "script"),
    [
        pytest.param("print(1)", "keeper", TRIED, id="a run of another script"),
        pytest.param(TRIED, "helper", TRIED, id="another principal's run"),
        pytest.param(FAILS, "keeper", FAILS, id="a run that failed"),
        pytest.param(None, "keeper", TRIED, id="a run the broker never saw"),
    ],
)
async def test_keeping_a_run_that_isnt_its_scripts_success_is_refused(
    skills: Skills, faker: Faker, ran: str | None, ran_by: str, script: str
) -> None:
    """
    GIVEN: A request to keep a function that names a run of a different script, a
           run by another principal, a run that failed, or a run the broker never saw
     WHEN: it is kept
     THEN: it is refused, and nothing is kept
    """
    # A run's id is 32 hex digits, so one made up here is a run the broker never saw.
    #
    run = faker.hexify("^" * 32)
    if ran is not None:
        run, _ = await skills.run(ran, principal=ran_by)

    with pytest.raises(MajiError) as caught:
        await skills.keep(script=script, run=run)
    assert caught.value.code == "invalid_argument"
    assert skills.library.names() == []


async def test_a_summary_with_nothing_printable_is_refused(skills: Skills) -> None:
    """
    GIVEN: A summary of control characters and spaces alone
     WHEN: a skill is kept with it
     THEN: it is refused, since an agent would read nothing
    """
    with pytest.raises(MajiError) as caught:
        await skills.keep(summary="\x1b\x07 \t")
    assert caught.value.message == "summary is required"


###############################################################################
#
# Promoting one: an item only when asked, as risky as what the skill reaches.


async def test_promoting_a_skill_that_only_reads_is_an_ordinary_item(
    skills: Skills, tried: str, session: str, faker: Faker
) -> None:
    """
    GIVEN: A function that reads, kept from the run it succeeded in, with a summary
           holding a line break and a terminal escape
     WHEN: the agent asks for it to be promoted, with its reason
     THEN: one item asks you, of medium risk, with no reasons and no passkey of its
           own, since the skill only reads
      AND: its facts give the summary as one line of printable text, the signature,
           what it reads with, and what the run it came from called; its diff is the
           whole code, as a new file; its requester is the agent's principal,
           session and the run; and the agent's reason is its claim
    """
    why = faker.sentence()
    await skills.keep(
        run=tried, summary="Sorted keys\n of fake.list's\x1b[31m arguments."
    )

    asked = await skills.promote(why=why)
    item = skills.item(asked["item"])
    facts = {f["label"]: f["value"] for f in item["facts"]}

    # What the agent is told, and the item that waits for you: ordinary, with nothing
    # flagged.
    #
    with check:
        assert (asked["state"], asked["risk"], asked["writes"]) == (
            "waiting",
            "medium",
            [],
        )
    with check:
        assert asked["session_name"] == session_name(session)
    with check:
        assert item["state"] == "waiting"
    with check:
        assert item["kind"] == "promote" and item["title"] == "Promote skills.keys"
    with check:
        assert item["subject"] == "skills.keys · me/local/keys"
    with check:
        assert item["risk"] == "medium" and "risk_reasons" not in item
    with check:
        assert not any(f.get("flag") for f in item["facts"])

    # What the item shows you of the skill: what every agent will read, what it
    # reaches, and what its run did.
    #
    with check:
        assert facts["Every agent will read"] == (
            "Sorted keys of fake.list's [31m arguments."
        )
    with check:
        assert facts["Signature"] == "skills.keys(limit: int = 2) -> list"
    with check:
        assert facts["Reads with"] == "fake.list"
    with check:
        assert facts["Ran"] == (
            f"the script it came from exited 0 in 1.2 s (run {tried[:8]}), "
            "calling fake.list once"
        )

    # Its code as a new file, who asked, and why, in the agent's own words.
    #
    with check:
        assert item["diff"]["new"] is True
    with check:
        assert {line["op"] for line in item["diff"]["lines"]} == {"add"}
    with check:
        assert item["requester"] == {
            "principal": "keeper",
            "session": session,
            "run": tried,
        }
    with check:
        assert item["claim"] == why


@pytest.mark.parametrize(
    ("name", "script", "basis", "why", "evidence", "writes", "unseen", "calls"),
    [
        pytest.param(
            "bump",
            WRITES,
            "found",
            "it writes with fake.update",
            WRITES_WITH,
            ["fake.update"],
            [],
            None,
            id="it writes",
        ),
        pytest.param(
            "bumps",
            CALLS_A_WRITER,
            "found",
            "it writes with fake.update",
            WRITES_WITH,
            ["fake.update"],
            [],
            {"label": "Calls", "value": "skills.bump"},
            id="a skill it calls writes",
        ),
        pytest.param(
            "pick",
            WORKS_OUT_A_NAME,
            "suspected",
            "what it reaches can't all be read from its code, so it may write",
            {
                "label": "Reaches what its code doesn't show",
                "value": UNSEEN,
                "flag": True,
            },
            [],
            [UNSEEN],
            None,
            id="it calls a stub by a name it works out",
        ),
    ],
)
async def test_promoting_a_skill_that_writes_or_may_write_is_high_risk(
    skills: Skills,
    faker: Faker,
    name: str,
    script: str,
    basis: str,
    why: str,
    evidence: dict[str, Any],
    writes: list[str],
    unseen: list[str],
    calls: dict[str, Any] | None,
) -> None:
    """
    GIVEN: A skill, kept from code alone, that writes with a stub the broker classes
           as a write, one that only calls that skill, or one that calls a stub by a
           name it works out at run time
     WHEN: the agent asks for it to be promoted
     THEN: it is high risk, because trust changes for later: found from its reach
           when it writes, directly or through the skill it calls, and suspected
           when its code can't show all it reaches, since it may write
      AND: the reason says which, and points at the fact that shows it, flagged: the
           stub it writes with, or the call its code can't show
      AND: the skill it calls is named among the facts, and the item is flagged as
           never having run
    """
    # `bumps` writes through `bump`, so `bump` is in the library in every case.
    #
    summary = faker.sentence()
    await skills.keep(script=WRITES, name="bump", summary=summary)
    kept = await skills.keep(script=script, name=name, summary=summary)

    asked = await skills.promote(name=name)
    item = skills.item(asked["item"])
    [reason] = item["risk_reasons"]
    facts = {f["label"]: f for f in item["facts"]}

    # What the agent is told of the skill's reach, when it keeps it and when it asks.
    #
    with check:
        assert kept["dynamic"] == unseen
    with check:
        assert (asked["risk"], asked["writes"]) == ("high", writes)

    # The one reason the item is high risk, and the flagged fact it points you at.
    #
    with check:
        assert (reason["code"], reason["basis"]) == ("trust_changes", basis)
    with check:
        assert reason["text"] == (
            f"Once promoted, any agent may run skills.{name}, and {why}"
        )
    with check:
        assert item["facts"][reason["evidence"][0]["fact"]] == evidence

    # What else the item shows: the skill it calls, if any, and that it never ran.
    #
    with check:
        assert facts.get("Calls") == calls
    with check:
        assert facts["Ran"]["flag"]
    with check:
        assert facts["Ran"]["value"].startswith("no: it was kept from code")


async def test_promoting_gives_the_skill_to_every_agent(
    skills: Skills, waiting: dict[str, Any]
) -> None:
    """
    GIVEN: A skill one agent kept, waiting for you to promote it
     WHEN: you promote it, and the broker reads the decision feed
     THEN: another agent's script calls it, the decision's outcome is recorded as
           applied, and that agent sees it in list_api, search_api and read_api,
           with its code
    """
    await skills.decide(waiting["item"], "promote")

    _, printed = await skills.run(CALLS, principal="helper")
    assert printed == KEYS
    assert skills.store.status_of(waiting["item"])["outcome"]["outcome"] == "applied"

    # What the other agent is shown of it.
    #
    services = skills.services
    listing = await services.control("list_api", principal="helper")
    with check:
        assert {"name": "skills.keys", "summary": SUMMARY} in listing["stubs"]
    with check:
        assert listing["namespaces"]["skills"] == "library"
    found = await services.control("search_api", principal="helper", query="sorted")
    with check:
        assert [f["name"] for f in found] == ["skills.keys"]
    doc = await services.control("read_api", principal="helper", name="skills.keys")
    with check:
        assert doc.startswith("skills.keys(limit: int = 2) -> list\n\nSorted keys")
    with check:
        assert "Reads with: fake.list\nPromoted: me/local/keys, version " in doc
    with check:
        assert doc.endswith('return sorted(fake.list(limit=limit)["args"])')


async def test_an_agents_newer_version_is_its_own_until_promoted(
    skills: Skills, promoted: dict[str, Any], other_session: str
) -> None:
    """
    GIVEN: A promoted skill
     WHEN: another agent keeps a changed version of it and asks for it to be promoted
     THEN: that agent gets its own version while the rest keep the promoted one
      AND: the item asks to promote a new version, shows the change against the
           promoted code, and says what it replaces
      AND: once you promote it, every agent gets the new version
    """
    # The agent that kept the changed version is given it, and the others the
    # promoted one.
    #
    helper = {"principal": "helper", "session": other_session}
    await skills.try_and_keep(BACKWARDS, **helper)
    seen = [
        (await skills.run(CALLS, principal=principal))[1]
        for principal in ("helper", "keeper", "third")
    ]
    assert seen == [KEYS_BACKWARDS, KEYS, KEYS]

    # The item for the new version shows it against the one promoted now.
    #
    asked = await skills.promote(**helper)
    item = skills.item(asked["item"])
    ops = [line["op"] for line in item["diff"]["lines"]]
    facts = {f["label"]: f["value"] for f in item["facts"]}
    with check:
        assert item["title"] == "Promote a new version of skills.keys"
    with check:
        assert "new" not in item["diff"]
    with check:
        assert ops.count("delete") == 1 and ops.count("add") == 1
    with check:
        assert "context" in ops
    with check:
        assert facts["Replaces"].startswith("version ")

    # Once you promote it, the agent that kept the earlier version is given it too.
    #
    await skills.decide(asked["item"], "promote")
    _, keeper = await skills.run(CALLS)
    assert keeper == KEYS_BACKWARDS


async def test_asking_again_replaces_the_item_waiting(
    skills: Skills, waiting: dict[str, Any]
) -> None:
    """
    GIVEN: A skill waiting for you to promote it
     WHEN: the agent asks again, then keeps a changed version, then asks again
     THEN: asking again for the same version is the same item, and nothing more is
           queued; keeping the changed version says the earlier one still waits
      AND: asking for the changed version withdraws the first item and waits on one
           of its own
    """
    again = await skills.promote()
    assert (again["item"], again["risk"]) == (waiting["item"], None)
    assert [i["id"] for i in skills.store.list_items("ahead").rows] == [waiting["item"]]

    kept = await skills.try_and_keep(REWORDED)
    assert kept["waiting"] == waiting["item"]
    second = await skills.promote()
    assert [i["id"] for i in skills.store.list_items("ahead").rows] == [second["item"]]
    assert skills.store.status_of(waiting["item"])["state"] == "withdrawn"


async def test_asking_for_changes_tells_the_agent_your_note(
    skills: Skills, waiting: dict[str, Any], other_session: str, faker: Faker
) -> None:
    """
    GIVEN: A skill waiting for you to promote it
     WHEN: you ask for changes with a note
     THEN: the agent reads your note with the skill, from any of its sessions, and
           is given it again when it keeps or asks to promote the next version
      AND: the agent can still call the version it kept while it changes it
    """
    note = faker.sentence()
    await skills.decide(waiting["item"], "request_changes", note=note)

    doc = await skills.services.control(
        "read_api", principal="keeper", name="skills.keys"
    )
    assert "Promotion: the user asked for changes" in doc
    assert f"The user's note: {note}" in doc
    _, printed = await skills.run(CALLS, session=other_session)
    assert printed == KEYS

    changed = TRIED.replace("as fake.list", "sorted, as fake.list")
    kept = await skills.try_and_keep(changed, session=other_session)
    assert kept["note"] == note
    assert (await skills.promote(session=other_session))["note"] == note


@pytest.mark.parametrize(
    "action",
    ["request_changes", "reject"],
    ids=["asking for changes", "declining to promote"],
)
async def test_a_note_reaches_the_agent_as_one_line_of_printable_text(
    skills: Skills, waiting: dict[str, Any], action: str
) -> None:
    """
    GIVEN: A skill waiting for you to promote it
     WHEN: you ask for changes, or decline to promote it, with a note that holds a
           line break and a terminal's escape character
     THEN: the agent reads your note with the skill as one line of printable text,
           so nothing in a note passes for a line of the skill's own description or
           reaches the agent's terminal as an escape
    """
    await skills.decide(waiting["item"], action, note="Sort\x1b[31m them\nfirst.")

    doc = await skills.services.control(
        "read_api", principal="keeper", name="skills.keys"
    )
    assert "The user's note: Sort [31m them first." in doc.splitlines()


async def test_declining_to_promote_leaves_the_skill_with_its_agent(
    skills: Skills, waiting: dict[str, Any]
) -> None:
    """
    GIVEN: A skill waiting for you to promote it
     WHEN: you reject it
     THEN: the agent that kept it can still call it, and no other agent can
    """
    await skills.decide(waiting["item"], "reject")
    assert (await skills.run(CALLS))[1] == KEYS
    _, refused = await skills.run("skills.keys()", principal="helper")
    assert refused.startswith("not_found: ")


async def test_a_promotion_overtaken_or_tampered_with_is_not_applied(
    skills: Skills, waiting: dict[str, Any], other_session: str, faker: Faker
) -> None:
    """
    GIVEN: Two agents' versions of one skill waiting, and a third version whose code
           is changed on disk after it was put to you
     WHEN: you promote the first, then the second, then the third
     THEN: the first is promoted; the second is a conflict, since another version
           was promoted after it was put to you; the third is a conflict, since its
           files changed; and the first stays promoted
    """
    # A second agent's version waits beside the first, and you promote both in turn.
    #
    helper = {"principal": "helper", "session": other_session}
    await skills.try_and_keep(REWORDED, **helper)
    second = await skills.promote(**helper)
    await skills.decide(waiting["item"], "promote")
    await skills.decide(second["item"], "promote")

    # A third agent's version is changed on disk between its item and your decision.
    #
    third = {"principal": "third", "session": faker.sha256()}
    tampered = await skills.keep(script=TRIED.replace("few", "many"), **third)
    asked = await skills.promote(**third)
    code = skills.library.root / "me/local/keys" / tampered["version"] / "skill.py"
    code.write_text(code.read_text() + "\nimport os\n")
    await skills.decide(asked["item"], "promote")

    outcomes = [
        skills.store.status_of(i)["outcome"]
        for i in (waiting["item"], second["item"], asked["item"])
    ]
    assert [o["outcome"] for o in outcomes] == ["applied", "conflict", "conflict"]
    assert "another version of skills.keys was promoted" in outcomes[1]["detail"]
    assert "files changed" in outcomes[2]["detail"]
    promoted = skills.library.promoted("keys")
    assert promoted is not None and promoted.version == waiting["version"]


###############################################################################
#
# Demoting a skill.


async def test_a_demoted_version_runs_for_no_agent_the_one_that_kept_it_included(
    skills: Skills, tried: str, kept: dict[str, Any], promoted: dict[str, Any]
) -> None:
    """
    GIVEN: A promoted skill, called by the agent that kept it and by another
     WHEN: you demote it with a note, and the broker reads the decision feed
     THEN: the demotion is applied, and neither agent's script can call that version:
           each is told you demoted it, when, your note, and how to run one again
      AND: neither agent sees it in list_api; read_api says it was demoted
      AND: the agent that kept it, keeping the same code again, is told it was
           demoted, and can't ask to promote it
      AND: a changed version it keeps runs for it, as its own, and says the
           earlier version was demoted
    """
    # While it is promoted, an agent that never kept it calls it. Your note holds a
    # terminal escape and a line break, which no agent is to be sent.
    #
    assert (await skills.run(CALLS, principal="helper"))[1] == KEYS
    await skills.demote(promoted["item"], note="It\x1b[31m lists keys\ntwice.")

    # The demotion is applied, and says which version no agent runs now.
    #
    outcome = skills.store.status_of(promoted["item"])["outcome"]
    with check:
        assert outcome["outcome"] == "applied"
    with check:
        assert outcome["detail"] == (
            f"demoted skills.keys, version {kept['version']}: no agent runs it now"
        )

    # Neither agent can call it or sees it listed. Each is told who demoted it and
    # when, your note as one line of printable text, and how to run one again.
    #
    today = skills.item(promoted["item"])["decision"]["at"][:10]
    told = (
        f"not_found: the user demoted skills.keys, version {kept['version']}, on "
        f"{today}: It [31m lists keys twice. No agent runs that version now; keep a "
        "changed version with save_skill to run one again"
    )
    for principal in ("keeper", "helper"):
        _, printed = await skills.run(CALLS, principal=principal)
        with check:
            assert printed == told
        listing = await skills.services.control("list_api", principal=principal)
        with check:
            assert "skills.keys" not in [s["name"] for s in listing["stubs"]]
    doc = await skills.services.control(
        "read_api", principal="keeper", name="skills.keys"
    )
    with check:
        assert doc.startswith(
            f"skills.keys: the user demoted skills.keys, version {kept['version']}"
        )

    # Keeping the same code again doesn't bring that version back, and it can't be
    # put to you again.
    #
    again = await skills.keep(run=tried)
    with check:
        assert again["state"] == "demoted" and again["version"] == kept["version"]
    with check:
        assert again["note"] == "It [31m lists keys twice."
    described = format_saved(again)
    with check:
        assert f"The user demoted this version on {today}" in described
    with check:
        assert "yours to call now" not in described
    with pytest.raises(MajiError, match="the user demoted skills.keys"):
        await skills.promote()

    # A changed version is the agent's own to run, and says what became of the
    # earlier one.
    #
    assert (await skills.try_and_keep(BACKWARDS))["state"] == "kept"
    assert (await skills.run(CALLS))[1] == KEYS_BACKWARDS
    doc = await skills.services.control(
        "read_api", principal="keeper", name="skills.keys"
    )
    assert f"Earlier, the user demoted skills.keys, version {kept['version']}" in doc


async def test_a_demotion_of_a_version_that_isnt_promoted_is_a_conflict(
    skills: Skills, kept: dict[str, Any], waiting: dict[str, Any]
) -> None:
    """
    GIVEN: A promotion whose files change before the broker applies it, with your
           demotion recorded on top of it before the broker reads either
     WHEN: the broker reads the decision feed, and reads it again
     THEN: the promotion is a conflict, and so is the demotion, saying no version is
           promoted; reading the feed again records nothing more
    """
    # The version's files change after it was put to you, so promoting it can't be
    # applied.
    #
    code = skills.library.root / "me/local/keys" / kept["version"] / "skill.py"
    code.write_text(code.read_text() + "\nimport os\n")

    # You promote it, then demote it, before the broker has read either decision.
    #
    promotion = skills.record(waiting["item"], "promote")
    skills.record(waiting["item"], "demote", supersedes=promotion["id"])

    assert await skills.feed.catch_up() == 2
    assert await skills.feed.catch_up() == 0

    # Neither decision could be applied, and no version was ever revoked.
    #
    decided = skills.item(waiting["item"])
    assert decided["decision"]["applied"]["outcome"] == "conflict"
    assert decided["decision"]["applied"]["detail"] == (
        f"skills.keys, version {kept['version']}, isn't the promoted version now: "
        "none is"
    )
    assert decided["superseded"]["applied"]["outcome"] == "conflict"
    assert skills.library.revoked("keys") == []


async def test_a_promotion_waiting_on_a_version_since_demoted_still_applies(
    skills: Skills, promoted: dict[str, Any], other_session: str
) -> None:
    """
    GIVEN: A promoted skill, and another agent's changed version waiting to replace it
     WHEN: you demote the promoted version, then promote the one waiting
     THEN: the new version is promoted, since the version it replaces is gone, not
           overtaken, and every agent gets it
    """
    helper = {"principal": "helper", "session": other_session}
    await skills.try_and_keep(BACKWARDS, **helper)
    second = await skills.promote(**helper)

    await skills.demote(promoted["item"])
    await skills.decide(second["item"], "promote")

    assert skills.store.status_of(second["item"])["outcome"]["outcome"] == "applied"
    assert (await skills.run(CALLS, principal="third"))[1] == KEYS_BACKWARDS


@pytest.mark.usefixtures("promoted")
async def test_a_principal_not_allowed_a_skill_can_neither_keep_promote_nor_call_it(
    skills: Skills,
) -> None:
    """
    GIVEN: A principal whose policy doesn't allow `skills.*`, and a promoted skill
     WHEN: it keeps a skill, asks to promote one, and a script of its calls the
           promoted one
     THEN: each is refused as not permitted
    """
    with pytest.raises(MajiError) as keeping:
        await skills.keep(principal="reader")
    assert keeping.value.code == "permission_denied"
    with pytest.raises(MajiError) as asking:
        await skills.promote(principal="reader")
    assert asking.value.code == "permission_denied"
    _, refused = await skills.run("skills.keys()", principal="reader")
    assert refused == "permission_denied: reader may not call skills.keys"


###############################################################################
#
# Calling skills.


async def test_a_skill_calls_another_and_fails_showing_its_own_lines(
    skills: Skills,
) -> None:
    """
    GIVEN: A skill that calls another skill, and a skill that raises
     WHEN: scripts call them
     THEN: the first gets the other's result, and the failure's traceback names the
           skill's own file and shows the line that raised
    """
    await skills.keep(script="def two() -> int:\n    return 2\n", name="two")
    outer = await skills.keep(
        script="def four() -> int:\n    return skills.two() * 2\n", name="four"
    )
    boom = await skills.keep(
        script="def boom() -> int:\n    raise ValueError('nope')\n", name="boom"
    )
    assert outer["reads"] == []
    _, printed = await skills.run("print(skills.four())")
    assert printed == "4"

    script = (
        "import traceback\ntry:\n    skills.boom()\nexcept ValueError:\n"
        "    print(traceback.format_exc())\n"
    )
    _, printed = await skills.run(script)
    assert f'File "<skill me/local/boom@{boom["version"]}>", line 2, in boom' in printed
    assert "raise ValueError('nope')" in printed


async def test_skill_loads_count_against_the_principals_rate_limit(
    skills: Skills,
) -> None:
    """
    GIVEN: A principal allowed two calls a minute in each namespace
     WHEN: its script asks for skills that don't exist three times
     THEN: the first two are told there's no such skill, and the third is rate
           limited before the library is searched
    """
    skills.services.broker.policy.grants["slow"] = Grant(
        allow=("skills.*",), per_minute=2
    )
    script = """
for _ in range(3):
    try:
        skills.nope()
    except maji.StubError as exc:
        print(exc.code)
"""
    _, printed = await skills.run(script, principal="slow")
    assert printed.split() == ["not_found", "not_found", "rate_limited"]
