"""
Tests for the runner service (`src/maji_mcp/runner/server.py`).

The first tests are of a run from start to finish: registered with the broker, run,
and finished. The rest are of a run that asks to mount paths: the runner resolves
them, the broker asks the control plane's grants, and paths not granted become one
queue item.

Everything is real except Docker: the runner service, the broker and the control
plane's socket for it, with a stand-in runner that records what it would have mounted.
A temporary folder stands in for your home directory.
"""

# system imports
import asyncio
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.broker.mounts import MountGrants
from maji_mcp.common.names import session_name
from maji_mcp.common.protocol import MajiError, call_unix
from maji_mcp.control.store import ControlStore
from maji_mcp.runner import server
from maji_mcp.runner.mounts import MountRules
from maji_mcp.runner.sandbox import Cover, Mount
from maji_mcp.runner.server import RunnerService
from tests.support.control import Plane
from tests.support.gateway import InProcessRunner
from tests.support.services import Services


###############################################################################
# A run, from the request on the runner's socket to the broker being told it is over.
#
async def test_a_run_is_registered_run_and_finished(
    services: Services, runner_socket: Path, fake_runner: InProcessRunner
) -> None:
    """
    GIVEN: The gateway asks the runner to run a script for a principal
     WHEN: the script calls a stub
     THEN: the call succeeds with the token the broker issued for the run, the result
           carries the run's id, and afterwards the token no longer works
    """
    result = await call_unix(
        runner_socket,
        "run",
        {"principal": "reader", "code": "print(fake.read(key='x')['args'])"},
    )
    assert result["stdout"] == "{'key': 'x'}\n"
    token = fake_runner.tokens[-1]
    with pytest.raises(MajiError) as caught:
        await call_unix(services.call_sock, "describe", {"token": token})
    assert caught.value.code == "unauthenticated"
    assert services.broker.runlog.calls(result["run_id"]) == [("fake.read", "ok")]


async def test_a_run_is_finished_even_when_it_fails_to_start(
    services: Services, runner_socket: Path, fake_runner: InProcessRunner
) -> None:
    """
    GIVEN: The Docker runner fails before the script runs
     WHEN: the runner service handles a run
     THEN: it still finishes the run with the broker, so no token stays live
    """
    fake_runner.fail = True
    with pytest.raises(MajiError):
        await call_unix(
            runner_socket, "run", {"principal": "reader", "code": "print(1)"}
        )
    assert len(services.broker.registry) == 0


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"principal": "reader", "code": ""}, "code is required"),
        ({"principal": "reader", "code": "x" * 600_000}, "limited"),
        ({"principal": "reader", "code": "1", "runtime": "ruby"}, "unknown runtime"),
        ({"principal": "reader", "code": "1", "timeout_s": 9999}, "timeout_s"),
        ({"code": "print(1)"}, "principal"),
    ],
    ids=[
        "no script",
        "too large a script",
        "an unknown runtime",
        "a timeout over the ceiling",
        "no principal",
    ],
)
async def test_a_bad_run_request_is_refused_before_any_run(
    services: Services,
    runner_socket: Path,
    params: dict[str, Any],
    message: str,
) -> None:
    """
    GIVEN: A run request with no script, too large a script, an unknown runtime, a
           timeout over the ceiling, or no principal
     WHEN: the runner service receives it
     THEN: it refuses with invalid_argument, and no run is registered
    """
    with pytest.raises(MajiError) as caught:
        await call_unix(runner_socket, "run", params)
    assert caught.value.code == "invalid_argument"
    assert message in caught.value.message
    assert len(services.broker.registry) == 0


async def test_a_run_a_stopping_runner_ends_is_unavailable_and_still_finished(
    services: Services, runner_socket: Path, fake_runner: InProcessRunner
) -> None:
    """
    GIVEN: A runner that has been told to stop
     WHEN: a run it was asked for is ended, or refused, because it is stopping
     THEN: the gateway is told the runner is unavailable and to run the script again
      AND: the run is still finished with the broker, so its token doesn't stay live
    """
    fake_runner.stopping = True

    with pytest.raises(MajiError) as caught:
        await call_unix(runner_socket, "run", {"principal": "reader", "code": "1"})

    with check:
        assert caught.value.code == "unavailable"
    with check:
        assert "the runner is stopping" in caught.value.message
    with check:
        assert "run it again" in caught.value.message
    with check:
        assert len(services.broker.registry) == 0


async def test_runs_an_earlier_runner_left_are_finished_with_the_broker(
    services: Services, runner_service: RunnerService
) -> None:
    """
    GIVEN: A run the broker still holds a token for, whose runner has gone, and the
           id of a run the broker has never heard of
     WHEN: a new runner tells the broker the runs it found left behind are over
     THEN: the token of the run the broker knew stops working
      AND: the run the broker doesn't know changes nothing, and stops nothing
    """
    run_id, token = await services.start_run("reader")

    await runner_service.finish_left(["no-such-run", run_id])

    with pytest.raises(MajiError) as refused:
        await call_unix(services.call_sock, "describe", {"token": token})
    assert refused.value.code == "unauthenticated"
    assert len(services.broker.registry) == 0


async def test_a_stopping_runner_waits_for_the_runs_in_flight(
    runner_service: RunnerService,
    runner_socket: Path,
    fake_runner: InProcessRunner,
    tmp_path: Path,
) -> None:
    """
    GIVEN: A run in flight on the runner's socket
     WHEN: the runner waits for its runs to drain, as it does when told to stop
     THEN: the wait gives up at its bound while the run is in flight, saying so
      AND: once the run has ended and been answered, the wait says none is in flight
    """
    # The script runs until a file appears, so the test decides when it ends.
    #
    go = tmp_path / "go"
    code = (
        "import os, time\n"
        f"while not os.path.exists({str(go)!r}):\n"
        "    time.sleep(0.01)\n"
        "print('done')"
    )
    running = asyncio.create_task(
        call_unix(runner_socket, "run", {"principal": "reader", "code": code})
    )
    async with asyncio.timeout(5):
        while not fake_runner.tokens:
            await asyncio.sleep(0.01)

    assert await runner_service.drained(0.2) is False

    go.touch()
    assert await runner_service.drained(5) is True
    assert (await running)["stdout"] == "done\n"


###############################################################################
# A run that asks to mount paths.
#
# What `Flow.run` runs as when a test doesn't say: the flow's own session.
#
OWN: Any = object()


class Flow:
    """
    A runner service and the control plane behind its broker.

    Attributes:
        service: the runner service a test runs scripts through
        fake: the stand-in for Docker, which records what each run would mount
        store: the control plane's store, where grants are asked for and decided
        home: the folder that stands in for your home directory
        never: where the never-mount list is read from. It doesn't exist until a
            test writes it.
        session: the session a run is in unless the test says otherwise
        other_session: a second session, for what doesn't carry across sessions
    """

    def __init__(
        self,
        service: RunnerService,
        fake: InProcessRunner,
        store: ControlStore,
        home: Path,
        never: Path,
        sessions: tuple[str, str],
    ) -> None:
        self.service = service
        self.fake = fake
        self.store = store
        self.home = home
        self.never = never
        self.session, self.other_session = sessions

    async def run(
        self,
        *paths: Path,
        session: str | None = OWN,
        why: str | None = None,
        client: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run a script that asks for `paths`, in the flow's own session unless told."""
        params: dict[str, Any] = {
            "principal": "keeper",
            "code": "print('ran')",
            "paths": [str(p) for p in paths],
        }
        if session is OWN:
            session = self.session
        if session:
            params["session"] = session
        if why:
            params["why"] = why
        if client:
            params["client"] = client
        result: dict[str, Any] = await self.service.run(params)
        return result

    async def ask(self, *paths: Path, session: str | None = OWN) -> str:
        """Run a script that asks for `paths`, and return the item it waits on."""
        item: str = (await self.run(*paths, session=session))["pending"]["items"][0]
        return item

    def decide(self, item_id: str, **choice: Any) -> None:
        """Record your decision on an item, as the control plane does with a passkey."""
        item = self.store.get_item(item_id)
        assert item is not None
        self.store.record_decisions(
            [{"item": item_id, "content_hash": item["content_hash"], **choice}],
            {"kind": "you", "passkey": "k"},
        )

    def grant(self, item_id: str) -> None:
        """Approve every path an item asks for, each remembered for its session."""
        item = self.store.get_item(item_id)
        assert item is not None
        self.decide(
            item_id,
            action="approve",
            remember=[
                {"pattern": pattern["id"], "lasts": "session"}
                for pattern in item["patterns"]
            ],
        )

    def waiting(self) -> list[dict[str, Any]]:
        """The items waiting in your queue."""
        return self.store.list_items("ahead").rows


@pytest.fixture
def flow(
    services: Services, plane: Plane, tmp_path: Path, session: str, other_session: str
) -> Flow:
    """
    The runner service in front of a broker that asks a control plane's store for
    mount grants, with a home directory holding a repository, an older copy beside
    it, and folders for notes, keys and other work.
    """
    home = tmp_path / "home"
    for folder in (
        "src/api/.git",
        "src/api/rules",
        "src/api-old",
        "notes",
        ".ssh",
        "other",
    ):
        (home / folder).mkdir(parents=True)

    services.broker.mounts = MountGrants(plane.broker_socket)
    fake = InProcessRunner(services)
    service = RunnerService(
        fake,  # type: ignore[arg-type]
        services.control_sock,
        mount_rules=MountRules.of(
            [home], never_file=tmp_path / "never-mount", home=home
        ),
    )
    never = tmp_path / "never-mount"
    return Flow(service, fake, plane.store, home, never, (session, other_session))


async def test_paths_not_granted_raise_one_item_and_the_script_doesnt_run(
    flow: Flow, faker: Faker
) -> None:
    """
    GIVEN: A script asking to mount a repository and a folder, neither granted
     WHEN: it is run, and then run again before you decide
     THEN: it doesn't run; the result names one waiting item, which asks for both
           resolved paths as a checklist, with a mount pattern for each, the
           agent's words as its claim, and the agent's principal, session and the
           app it reported
      AND: the rerun names the same item, and nothing more is queued
    """
    api, notes = flow.home / "src" / "api", flow.home / "notes"
    app = {"name": faker.slug(), "version": faker.numerify("#.#.###")}
    why = faker.sentence()

    # The first run waits: it names one item and both paths, and nothing ran.
    #
    first = await flow.run(api, notes, why=why, client=app)
    resolved = [str(api.resolve()), str(notes.resolve())]
    assert first == {
        "pending": {"items": [first["pending"]["items"][0]], "paths": resolved}
    }
    assert flow.fake.tokens == []

    # What the item tells you. Each fact is checked on its own, so one run shows
    # every one that is wrong.
    #
    [item] = flow.waiting()
    with check:
        assert (item["kind"], item["title"]) == ("grant", "Mount 2 paths read-only")
    with check:
        assert item["claim"] == why
    with check:
        assert item["requester"] == {
            "principal": "keeper",
            "session": flow.session,
            "client": app,
        }
    with check:
        assert item["blocking"] is True
    with check:
        assert [p["label"] for p in item["parts"]] == resolved
    with check:
        assert [(p["access"], p["resource"], p["match"]) for p in item["patterns"]] == [
            ("mount", path, {"path": path}) for path in resolved
        ]

    # A rerun before you decide waits on the same item.
    #
    again = await flow.run(api, notes)
    assert again == first
    assert len(flow.waiting()) == 1


async def test_once_granted_the_script_runs_with_the_paths_mounted(flow: Flow) -> None:
    """
    GIVEN: A script whose two paths you granted for this session
     WHEN: it is run again in the same session, and in another session
     THEN: in the same session it runs, with each path bound read-only under its
           name, granted by the broker, and the result says where each is mounted
      AND: in another session it waits on a new item, since the grant was for one
           session
    """
    api, notes = flow.home / "src" / "api", flow.home / "notes"
    item = await flow.ask(api, notes)
    flow.grant(item)

    ran = await flow.run(api, notes)
    resolved = {"api": str(api.resolve()), "notes": str(notes.resolve())}
    assert ran["stdout"] == "ran\n"
    assert ran["mounts"] == resolved
    assert flow.fake.mounts == [
        (
            [Mount(resolved["api"], "api"), Mount(resolved["notes"], "notes")],
            set(resolved.values()),
        )
    ]

    elsewhere = await flow.run(api, notes, session=flow.other_session)
    assert elsewhere["pending"]["items"] != [item]


async def test_a_refused_path_isnt_asked_for_again_this_session(flow: Flow) -> None:
    """
    GIVEN: A script asking for two paths, where you granted one and refused the other
     WHEN: it is run again with both, and then with only the granted one
     THEN: with both, it is refused, naming the item and the path you refused, and
           nothing new is queued
      AND: with only the granted one, it runs
    """
    api, notes = flow.home / "src" / "api", flow.home / "notes"
    item = await flow.ask(api, notes)
    flow.decide(
        item,
        action="approve",
        parts={"p1": "approve", "p2": "deny"},
        remember=[{"pattern": "mount-p1", "lasts": "session"}],
    )

    with pytest.raises(MajiError) as refused:
        await flow.run(api, notes)
    assert refused.value.code == "permission_denied"
    assert refused.value.data == {"item": item, "path": str(notes.resolve())}
    assert flow.waiting() == []

    ran = await flow.run(api)
    assert ran["mounts"] == {"api": str(api.resolve())}


async def test_a_refusal_says_in_this_session_only_when_there_is_one(
    flow: Flow,
) -> None:
    """
    GIVEN: A path you refused for an agent in a session, and the same path refused
           for an agent whose app gave no session
     WHEN: each runs its script again
     THEN: both are refused; the first is told it isn't asked for again in this
           session, by the session's name, and the second that it isn't asked for
           again, with no session to end the refusal, until you let it be
    """
    notes = flow.home / "notes"
    for session in (flow.session, None):
        item = await flow.ask(notes, session=session)
        flow.decide(item, action="deny")
    with pytest.raises(MajiError) as in_session:
        await flow.run(notes)
    with pytest.raises(MajiError) as without:
        await flow.run(notes, session=None)
    assert (
        f"isn't asked for again in this session, {session_name(flow.session)}, unless"
        in in_session.value.message
    )
    assert "isn't asked for again unless they let it be" in without.value.message


async def test_a_refused_path_is_asked_for_again_once_you_let_it(flow: Flow) -> None:
    """
    GIVEN: A path you refused, which the agent's rerun was refused for
     WHEN: you let it ask again from the item, and the agent runs the script again
     THEN: the rerun waits on a new item asking for the path, rather than being
           refused
      AND: once you refuse that one too, the next rerun is refused again, naming the
           new item, so letting it ask again lifts one refusal and no more
    """
    notes = flow.home / "notes"
    first = await flow.ask(notes)
    flow.decide(first, action="deny")
    with pytest.raises(MajiError):
        await flow.run(notes)

    flow.store.let_ask_again(first, "passkey:k")
    second = await flow.ask(notes)
    assert second != first
    assert [i["id"] for i in flow.waiting()] == [second]

    flow.decide(second, action="deny")
    with pytest.raises(MajiError) as refused:
        await flow.run(notes)
    assert refused.value.code == "permission_denied"
    assert refused.value.data == {"item": second, "path": str(notes.resolve())}


async def test_a_refused_folder_doesnt_stop_a_request_for_one_inside_it(
    flow: Flow,
) -> None:
    """
    GIVEN: A folder you refused as too broad for what the agent was after
     WHEN: the agent, told where to look, asks for a folder inside it
     THEN: the request comes to you as a new item, rather than being refused: a
           refusal covers only the path refused
    """
    api = flow.home / "src" / "api"
    broad = await flow.ask(api)
    flow.decide(broad, action="deny")

    narrow = await flow.run(api / "rules")
    assert narrow["pending"]["items"] != [broad]
    assert narrow["pending"]["paths"] == [str((api / "rules").resolve())]


async def test_a_grant_on_a_folder_covers_the_folders_in_it(flow: Flow) -> None:
    """
    GIVEN: A repository you granted for this session
     WHEN: a script asks for a folder inside it, and one beside it whose name starts
           the same way
     THEN: the folder inside it is mounted without asking you, under its own name
      AND: the one beside it waits on a new item, since a grant covers what is in
           its folder and nothing else
    """
    api = flow.home / "src" / "api"
    flow.grant(await flow.ask(api))

    ran = await flow.run(api / "rules")
    assert ran["mounts"] == {"rules": str((api / "rules").resolve())}

    beside = await flow.run(flow.home / "src" / "api-old")
    assert beside["pending"]["paths"] == [
        str((flow.home / "src" / "api-old").resolve())
    ]


async def test_a_mount_kept_for_hours_of_a_session_lapses_then(flow: Flow) -> None:
    """
    GIVEN: A path you granted for this session, ending in 8 hours at the latest
     WHEN: the agent runs a script over it in that session, then again after 8 hours
           while the session goes on
     THEN: the first runs, and the second waits on a new item: the grant ended with
           the working day, though the session didn't
    """
    notes = flow.home / "notes"
    item = await flow.ask(notes)
    until = datetime.fromtimestamp(time.time() + 8 * 3600, UTC).isoformat()
    flow.decide(
        item,
        action="approve",
        remember=[{"pattern": "mount-p1", "lasts": "session", "until": until}],
    )
    assert (await flow.run(notes))["stdout"] == "ran\n"

    later = time.time() + 8 * 3600 + 60
    flow.store.clock = lambda: later
    assert "pending" in await flow.run(notes)


async def test_your_never_mount_list_narrows_even_what_you_granted(
    flow: Flow,
) -> None:
    """
    GIVEN: A repository you granted for this session, so the folders in it are
           covered too
     WHEN: you add a folder in it to your never-mount list, and the agent asks for
           that folder, then for the repository; then the list gets a line it can't
           use
     THEN: the listed folder is refused, whatever the grant says, and nothing is
           queued
      AND: the repository is mounted with the listed folder covered, so a script
           reads the rest of it; the result counts what was left out without naming
           what is on your list
      AND: with the bad line, every mount is refused, even one granted and not
           listed, until the list is fixed
    """
    api = flow.home / "src" / "api"
    flow.grant(await flow.ask(api))
    assert "stdout" in await flow.run(api / "rules")

    flow.never.write_text("~/src/api/rules\n")
    with pytest.raises(MajiError, match="never has mounted") as refused:
        await flow.run(api / "rules")
    assert refused.value.code == "permission_denied"
    assert flow.waiting() == []

    ran = await flow.run(api)
    assert ran["stdout"] == "ran\n"
    assert flow.fake.covers[-1] == [Cover("api", "rules", folder=True)]
    assert ran["left_out"] == {"api": {"files": [], "count": 1}}

    flow.never.write_text("~/notes\nsrc/web\n")
    with pytest.raises(MajiError, match="fixes their never-mount list"):
        await flow.run(api)


async def test_a_folder_is_put_to_you_with_what_its_mount_leaves_out(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    GIVEN: A repository holding a `.env` file and a key, and a folder holding neither
     WHEN: a script asks to mount both, is run again while the item waits, and again
           once you've granted both
     THEN: the item isn't high risk: a fact names the two files a mount of the
           repository leaves out, and the folder gets no fact
      AND: the rerun names the same item without looking through either again, so
           only a path about to be put to you is looked through before it is granted
      AND: once granted, the script runs with each of the two files covered, and the
           result names them, so an empty read isn't taken for an empty file
    """
    api, notes = flow.home / "src" / "api", flow.home / "notes"
    (api / ".env").write_text("TOKEN=x")
    (api / "deploy").mkdir()
    (api / "deploy" / "server.pem").write_text("")

    # Count the looks the runner makes, to show a rerun that waits makes none.
    #
    looked: list[Path] = []
    real = server.covers_in

    def counting(path: Path, *args: Any) -> Any:
        looked.append(path)
        return real(path, *args)

    monkeypatch.setattr(server, "covers_in", counting)

    # The first run looks through both paths, and the item says what it found.
    #
    first = await flow.run(api, notes)
    [item] = flow.waiting()
    assert item["risk"] == "medium" and not item.get("risk_reasons")
    assert item["facts"][2:] == [
        {
            "label": f"Left out of {api.resolve()}",
            "value": ".env, deploy/server.pem: each reads as empty to a script",
        }
    ]
    assert len(looked) == 2

    # A rerun while the item waits looks through neither again.
    #
    assert await flow.run(api, notes) == first
    assert len(looked) == 2

    # Once granted, the run covers the two files and its result names them.
    #
    flow.grant(item["id"])
    ran = await flow.run(api, notes)
    assert ran["stdout"] == "ran\n"
    assert sorted(flow.fake.covers[-1], key=lambda c: c.path) == [
        Cover("api", ".env"),
        Cover("api", "deploy/server.pem"),
    ]
    assert ran["left_out"] == {
        "api": {"files": [".env", "deploy/server.pem"], "count": 2}
    }


async def test_a_file_named_like_a_secret_needs_a_grant_of_its_own(flow: Flow) -> None:
    """
    GIVEN: A repository you granted for this session, holding a `.env` file
     WHEN: a script asks for the `.env` by name, alone and then with the repository
     THEN: the grant on the repository doesn't cover it: a new item asks for the
           file, high risk for secrets in reach, only suspected, pointing at a
           flagged fact that names the file
      AND: once you grant the file, a run asking for both mounts the file, and the
           mount of the repository no longer covers it
    """
    api = flow.home / "src" / "api"
    (api / ".env").write_text("TOKEN=x")
    env = str((api / ".env").resolve())
    flow.grant(await flow.ask(api))
    assert flow.fake.covers == []

    asked = await flow.run(api / ".env")
    assert asked["pending"]["paths"] == [env]
    [by_name] = flow.waiting()
    assert by_name["risk"] == "high"
    [reason] = by_name["risk_reasons"]
    assert (reason["code"], reason["basis"]) == ("secrets_in_reach", "suspected")
    [pointer] = reason["evidence"]
    fact = by_name["facts"][pointer["fact"]]
    assert fact["label"] == "Named like a secret" and fact["flag"] is True
    assert env in fact["value"]

    both = await flow.run(api, api / ".env")
    assert both["pending"]["paths"] == [env]

    flow.grant(by_name["id"])
    ran = await flow.run(api, api / ".env")
    assert ran["mounts"] == {"api": str(api.resolve()), "env": env}
    assert flow.fake.covers[-1] == []
    assert ran["left_out"] == {}

    alone = await flow.run(api)
    assert flow.fake.covers[-1] == [Cover("api", ".env")]
    assert alone["left_out"] == {"api": {"files": [".env"], "count": 1}}


async def test_a_folder_too_large_to_look_through_isnt_put_to_you(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    GIVEN: A folder too large to look through in the time allowed
     WHEN: a script asks to mount it
     THEN: it is refused as `invalid_argument`, telling the agent to ask for a folder
           inside it, and nothing is queued: a mount of it couldn't be sure to leave
           out what it holds that is named like a secret
    """
    (flow.home / "notes" / "monday.md").write_text("")
    monkeypatch.setattr(server, "LOOK_SECONDS", 0.0)
    with pytest.raises(MajiError, match="ask for a folder inside it") as refused:
        await flow.run(flow.home / "notes")
    assert refused.value.code == "invalid_argument"
    assert flow.waiting() == []


async def test_a_place_never_mounted_is_refused_before_anything_is_asked(
    flow: Flow,
) -> None:
    """
    GIVEN: A script asking to mount a hidden folder at the top of your home directory
     WHEN: it is run
     THEN: the runner refuses it as `permission_denied`, and nothing is queued
    """
    with pytest.raises(MajiError) as refused:
        await flow.run(flow.home / ".ssh")
    assert refused.value.code == "permission_denied"
    assert flow.waiting() == []


async def test_a_path_swapped_since_it_was_granted_isnt_mounted(flow: Flow) -> None:
    """
    GIVEN: A path the broker granted, which has since been replaced by a symlink to
           another folder
     WHEN: the runner resolves it again, just before creating the container
     THEN: it refuses with `conflict`, so the other folder isn't bound
    """
    api = (flow.home / "src" / "api").resolve()
    shutil.rmtree(api)
    api.symlink_to(flow.home / "other")
    with pytest.raises(MajiError) as swapped:
        flow.service._granted([api], [str(api)], flow.service.mount_rules)
    assert swapped.value.code == "conflict"


async def test_without_a_control_plane_or_mount_rules_nothing_is_mounted(
    services: Services, tmp_path: Path
) -> None:
    """
    GIVEN: A broker with no control plane, and a runner with no mount rules
     WHEN: each is asked for a mount
     THEN: the broker answers `unavailable`, and the runner `invalid_argument`
    """
    with pytest.raises(MajiError) as broker:
        await MountGrants(None).check("keeper", None, ["/h/src/api"])
    assert broker.value.code == "unavailable"
    service = RunnerService(InProcessRunner(services), services.control_sock)  # type: ignore[arg-type]
    with pytest.raises(MajiError) as runner:
        await service.run(
            {"principal": "keeper", "code": "1", "paths": [str(tmp_path)]}
        )
    assert runner.value.code == "invalid_argument"
