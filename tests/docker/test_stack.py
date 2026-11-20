"""
Tests of the whole stack: compose brings up the broker, runner, gateway and control
plane; an MCP client runs scripts through the gateway, as Claude Code would, and a
passkey is registered with the control plane as at bring-up.

The stack uses its own volume prefix, port, policy and secrets, so it can't touch a
stack you have running. It needs both images (`make images`), or other builds named
by MAJI_SERVICE_IMAGE and MAJI_RUNTIME_IMAGE. Its runner mounts paths only from a
folder of the test's own, never from your home directory as a whole.
"""

# system imports
import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 3rd party imports
import httpx
import httpx2
import pytest
from mcp import Client, ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation
from pytest_check import check

# project imports
from tests.support.contract import Authenticator
from tests.support.paths import REPO
from tests.support.repositories import build_repository

pytestmark = pytest.mark.docker


# The control plane's database in its container, and the instance the stack is
# brought up as.
#
CONTROL_DB = "/var/lib/maji-control/control.db"
INSTANCE = "e2e-test"

# The key the fake Sentry takes, and the organization it serves. Made up for the test.
#
SENTRY_KEY = "sntryu_" + "7e57" * 16
SENTRY_ORG = "acme"

# How long Docker gives a container to stop before killing it, and the exit code of a
# container it killed.
#
STOP_SECONDS = 10
KILLED = 137


@dataclass
class Stack:
    """
    A stack running under compose, and the ways a test reaches it.

    Attributes:
        url: the gateway's MCP endpoint
        token: the gateway token of the principal `tester`
        control: the control plane's origin, at its name `localhost`
        control_token: the control plane's command-line token
        compose: the `docker compose` command for this stack, to add a subcommand to
        env: the environment that names this stack's volumes, ports and secrets
        mounts: the folder the stack's runner may mount from
        never: where the stack's never-mount list is read from
    """

    url: str
    token: str
    control: str
    control_token: str
    compose: list[str]
    env: dict[str, str]
    mounts: Path
    never: Path

    async def call(self, tool: str, **args: object) -> tuple[bool, str]:
        """
        Call one gateway tool in a fresh MCP session, and return whether it failed,
        and its text.

        The session is opened and closed inside the test's own task: the MCP client
        holds an anyio task group, which must be exited in the task that entered it,
        and an async fixture's teardown runs in another.
        """
        async with (
            httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {self.token}"}, timeout=120
            ) as http,
            streamable_http_client(self.url, http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(tool, dict(args))
        text: str = result.content[0].text  # type: ignore[union-attr]
        return result.is_error, text

    async def text(self, tool: str, **args: object) -> str:
        """Call one gateway tool in a fresh MCP session, and return its text."""
        failed, text = await self.call(tool, **args)
        assert not failed, text
        return text

    def waiting(self, kind: str) -> list[dict[str, Any]]:
        """The items of one kind in your queue, read with the command-line token."""
        queue = httpx.get(
            f"{self.control}/api/v0/items?lane=ahead", headers=self.bearer
        )
        assert queue.status_code == 200, queue.text
        return [item for item in queue.json()["items"] if item["kind"] == kind]

    @property
    def bearer(self) -> dict[str, str]:
        """The header that carries the control plane's command-line token."""
        return {"Authorization": f"Bearer {self.control_token}"}

    def in_control(self, store_calls: str) -> None:
        """
        Run statements on the control plane's store, as `s`, in its own container.

        Deciding takes a passkey, so a test records a grant or a decision in the
        control plane's database directly, as setup does.
        """
        code = (
            "from maji_mcp.control.store import ControlStore; "
            f"s = ControlStore({CONTROL_DB!r}, {INSTANCE!r}); {store_calls}"
        )
        subprocess.run(
            [*self.compose, "exec", "-T", "control", "python", "-c", code],
            env=self.env,
            check=True,
            capture_output=True,
        )

    def log(self, service: str) -> str:
        """Everything one service has logged so far."""
        logs = subprocess.run(
            [*self.compose, "logs", "--no-log-prefix", service],
            env=self.env,
            capture_output=True,
            text=True,
        )
        return logs.stdout

    def wait_for_gateway(self, within: float = 30) -> None:
        """
        Wait until the gateway is serving: it answers a request without a token with
        401.
        """
        deadline = time.monotonic() + within
        while True:
            try:
                if httpx.post(self.url, json={}).status_code == 401:
                    return
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, "the gateway never came up"
            time.sleep(0.5)

    def stop(self, service: str) -> tuple[float, int]:
        """
        Ask one service to stop, as `docker compose stop` asks, and return how many
        seconds it took and the exit code its container ended with.
        """
        found = subprocess.run(
            [*self.compose, "ps", "-q", service],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        container = found.stdout.strip()
        started = time.monotonic()
        subprocess.run(
            [*self.compose, "stop", "-t", str(STOP_SECONDS), service],
            env=self.env,
            check=True,
            capture_output=True,
        )
        took = time.monotonic() - started
        ended = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.ExitCode}}", container],
            check=True,
            capture_output=True,
            text=True,
        )
        return took, int(ended.stdout)

    def executors(self) -> list[str]:
        """
        The ids of this stack's executor containers, running or not: the ones that
        mount its broker's call socket.
        """
        listed = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "-q",
                "--filter",
                "label=maji-mcp/run_id",
                "--filter",
                f"volume={self.env['MAJI_PREFIX']}-broker-call",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return listed.stdout.split()

    def health(self, within: float = 30) -> dict[str, Any]:
        """The control plane's health, waiting up to `within` seconds for an answer."""
        deadline = time.monotonic() + within
        while True:
            try:
                answer = httpx.get(f"{self.control}/api/v0/health")
                if answer.status_code == 200:
                    health: dict[str, Any] = answer.json()
                    return health
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, "the control plane didn't answer"
            time.sleep(0.5)


class FakeSentry(BaseHTTPRequestHandler):
    """
    A Sentry on this machine for the broker's container to reach: the organization and
    its projects, for `SENTRY_KEY` alone. Each request's path, and whether it carried
    that key, is kept in `seen`.
    """

    seen: list[tuple[str, bool]] = []

    def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
        keyed = self.headers.get("Authorization") == f"Bearer {SENTRY_KEY}"
        FakeSentry.seen.append((self.path, keyed))
        answers = {
            f"/api/0/organizations/{SENTRY_ORG}/": {"slug": SENTRY_ORG},
            f"/api/0/organizations/{SENTRY_ORG}/projects/": [
                {"slug": "api", "id": "42", "name": "API", "platform": "python"}
            ],
        }
        body = answers.get(self.path) if keyed else None
        self.send_response(200 if body is not None else (404 if keyed else 401))
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body if body is not None else {}).encode())

    def log_message(self, *args: Any) -> None:
        """Say nothing on the test's output."""


@pytest.fixture(scope="module")
def fake_sentry(unused_tcp_port_factory: Callable[[], int]) -> Iterator[str]:
    """
    The fake Sentry, serving on this machine's 127.0.0.1, as the address the broker's
    container reaches it at.
    """
    port = unused_tcp_port_factory()
    server = ThreadingHTTPServer(("127.0.0.1", port), FakeSentry)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        yield f"http://host.docker.internal:{port}"
    finally:
        server.shutdown()
        server.server_close()


@dataclass
class Owner:
    """
    The first passkey, registered at bring-up, and what bring-up saw on the way.

    Attributes:
        key: the passkey
        health_before: the control plane's health before any passkey existed
        registered: the control plane's answer to registering it
        code_again: how `maji-control setup-code` exited once a passkey existed
    """

    key: Authenticator
    health_before: dict[str, Any]
    registered: httpx.Response
    code_again: int


@pytest.fixture(scope="module")
def owner(stack: Stack) -> Owner:
    """
    The stack's first passkey, registered with the code the control plane's container
    gives, through the routes the setup page uses, as `make up` does. Tests that
    decide sign in with it.
    """
    health = stack.health()
    setup_code = [*stack.compose, "exec", "-T", "control", "maji-control", "setup-code"]
    made = subprocess.run(setup_code, env=stack.env, capture_output=True, text=True)
    assert made.returncode == 0, made.stderr

    origin = {"Origin": stack.control}
    options = httpx.post(
        f"{stack.control}/api/v0/passkeys/registration/options",
        json={"name": "e2e"},
        headers=origin,
    ).json()
    key = Authenticator()
    credential = key.register(options["public_key"], stack.control)
    registered = httpx.post(
        f"{stack.control}/api/v0/passkeys",
        json={
            "registration_id": options["registration_id"],
            "credential": credential,
            "setup_code": made.stdout.strip(),
        },
        headers=origin,
    )
    again = subprocess.run(setup_code, env=stack.env, capture_output=True, text=True)
    return Owner(key, health, registered, again.returncode)


@pytest.fixture(scope="module")
def stack(
    unused_tcp_port_factory: Callable[[], int], fake_sentry: str
) -> Iterator[Stack]:
    """
    The whole stack brought up under compose, with a volume prefix, ports, policy and
    secrets of its own, and taken down with its volumes afterwards.

    Bringing it up takes most of a minute, so the module shares one. Its principal
    `tester` may call the `maji` and `sentry` stubs and skills, and its broker's
    Sentry is the fake one on this machine.
    """
    if not shutil.which("docker"):
        pytest.skip("no docker command")
    suffix = uuid.uuid4().hex[:8]

    # Compose mounts secrets from the host, and Rancher Desktop shares only the home
    # directory into its VM, so the test's secrets live under it.
    #
    work = Path.home() / ".cache" / "maji-mcp-test" / suffix
    work.mkdir(parents=True)

    # The fixtures to mount sit beside the secrets rather than in them, since a
    # folder holding the secrets is never mounted. The runner's root is the folder
    # holding both: it has to see the secrets folder to refuse it, and doesn't start
    # if it can't.
    #
    mounts = work.with_name(f"{suffix}-mounts")
    mounts.mkdir()
    token = secrets.token_urlsafe(32)
    (work / "gateway_tokens.json").write_text(
        json.dumps(
            {"principals": {"tester": hashlib.sha256(token.encode()).hexdigest()}}
        )
    )
    (work / "emacs-org.token").write_text("")
    (work / "instance").write_text(f"{INSTANCE}\n")
    control_token = secrets.token_urlsafe(32)
    digest = "sha256:" + hashlib.sha256(control_token.encode()).hexdigest()
    (work / "control_clients.json").write_text(json.dumps({"clients": {"cli": digest}}))
    (work / "servers.json").write_text('{"servers": {}}')
    (work / "policy.json").write_text(
        json.dumps(
            {
                "principals": {
                    "tester": {
                        "allow": ["maji.*", "skills.*", "sentry.*"],
                        "per_minute": 60,
                    }
                }
            }
        )
    )
    port, control_port = unused_tcp_port_factory(), unused_tcp_port_factory()
    env = {
        **os.environ,
        "MAJI_PREFIX": f"maji-e2e-{suffix}",
        "MAJI_GATEWAY_PORT": str(port),
        "MAJI_CONTROL_PORT": str(control_port),
        "MAJI_SECRETS": str(work),
        "MAJI_POLICY": str(work / "policy.json"),
        "MAJI_SERVERS": str(work / "servers.json"),
        "MAJI_SECRETS_DIR": str(work),
        "MAJI_MOUNT_ROOT": str(work.parent),
        "MAJI_NEVER_MOUNT": str(work / "never-mount"),
        "SENTRY_URL": fake_sentry,
    }
    compose = ["docker", "compose", "-f", str(REPO / "compose.yaml")]
    stack = Stack(
        f"http://127.0.0.1:{port}/mcp",
        token,
        f"http://localhost:{control_port}",
        control_token,
        compose,
        env,
        mounts,
        work / "never-mount",
    )
    up = subprocess.run([*compose, "up", "-d"], env=env, capture_output=True, text=True)
    try:
        assert up.returncode == 0, up.stderr
        stack.wait_for_gateway()
        stack.health(within=60)
        yield stack
    finally:
        logs = subprocess.run(
            [*compose, "logs"], env=env, capture_output=True, text=True
        )

        # An executor isn't one of the stack's services, so `down` doesn't remove
        # it. One left by a runner that was killed would keep the broker's socket
        # volume from being removed, so it goes first.
        #
        for executor in stack.executors():
            subprocess.run(["docker", "rm", "-f", executor], capture_output=True)
        subprocess.run([*compose, "down", "-v"], env=env, capture_output=True)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(mounts, ignore_errors=True)
        print(logs.stdout[-4000:])


def test_the_broker_lists_its_services_once_the_stack_is_up(stack: Stack) -> None:
    """
    GIVEN: A stack brought up from nothing, whose broker and control plane start
           together, so either can be first
     WHEN: the Connections list is read
     THEN: it lists Sentry, Jira and GitHub, none connected, each key sent only to
           the host the broker is configured with: the broker reported its services
           once the control plane answered
    """
    deadline = time.monotonic() + 30
    while True:
        listed = httpx.get(
            f"{stack.control}/api/v0/connections", headers=stack.bearer
        ).json()["connections"]
        if listed:
            break
        assert time.monotonic() < deadline, "the broker never reported its services"
        time.sleep(0.5)

    assert sorted((c["service"], c["status"], c["host"]) for c in listed) == [
        ("github", "not_connected", "api.github.com"),
        ("jira", "not_connected", "api.atlassian.com"),
        ("sentry", "not_connected", "host.docker.internal"),
    ]


async def test_a_script_mounts_a_path_once_it_is_granted(stack: Stack) -> None:
    """
    GIVEN: The stack running under compose, and a repository in the folder its runner
           may mount from, holding a `.env` file
     WHEN: run_python asks to mount the repository, before and after a grant covers it
     THEN: before, the script doesn't run: the result says what it waits on, and the
           control plane's queue holds an item asking for the repository, with the
           agent's words as its claim, saying the `.env` is left out, and not high
           risk
      AND: after, the script reads the repository's history with maji.git_log and
           reads the `.env` as empty, and the result says where the repository was
           mounted and that the `.env` was left out
      AND: once you put the repository on your never-mount list, it isn't mounted,
           whatever the grant says
    """
    repo = stack.mounts / "api"
    build_repository(repo)
    (repo / ".env").write_text("TOKEN=only-on-the-host\n")
    path = os.path.realpath(repo)
    code = (
        "print([c['subject'] for c in maji.git_log('api')])\n"
        "print(repr(open('/mnt/api/.env').read()))"
    )
    asking = {"code": code, "paths": [str(repo)], "why": "The join reads its history."}

    # Before a grant, the script waits, and your queue holds the item it waits on.
    #
    waiting = await stack.text("run_python", **asking)
    assert waiting.startswith(f"[not run: the user hasn't granted {path}.")
    [item] = stack.waiting("grant")
    assert item["title"] == f"Mount {path} read-only"
    assert item["claim"] == "The join reads its history."
    assert item["risk"] == "medium"
    assert item["facts"][-1] == {
        "label": f"Left out of {path}",
        "value": ".env: each reads as empty to a script",
    }

    # With a grant on the repository, the same request runs, the `.env` covered.
    #
    pattern = {
        "id": "mount",
        "summary": f"Read-only mounts of {path}",
        "access": "mount",
        "resource": path,
        "match": {"path": path},
    }
    stack.in_control(f"s.add_grant('tester', {pattern!r}, 'permanent')")

    ran = await stack.text("run_python", **asking)
    lines = ran.splitlines()
    assert lines[0] == "['ABC-3 shout three', 'ABC-2 shout two', 'ABC-1 start the app']"
    assert lines[1] == "''"
    assert lines[2] == "[left out of /mnt/api, read as empty: .env]"
    assert "mounted /mnt/api" in lines[-1]

    # Your never-mount list is read on each run, and wins over the grant.
    #
    stack.never.write_text(f"{path}\n")
    try:
        failed, refused = await stack.call("run_python", **asking)
    finally:
        stack.never.unlink()
    assert failed
    assert "never has mounted" in refused


TRIED = """
def doubled(n: int) -> int:
    \"\"\"Twice n.\"\"\"
    return n * 2


print(doubled(21))
"""


async def test_a_kept_skill_runs_in_later_sessions_and_is_promoted_on_request(
    stack: Stack,
) -> None:
    """
    GIVEN: The stack running under compose, and an agent that ran a script defining
           a function
     WHEN: it keeps the function as a skill by naming that run, calls it from a new
           session, then asks for it to be promoted, and you promote it, then demote it
     THEN: keeping it puts nothing in your queue, and the new session calls it at
           once, its result naming the skill it called
      AND: asking puts one ordinary item in your queue, its code as a new file;
           once you promote it, the broker reads your decision and the skill reads
           as promoted, and the decided item offers to demote it
      AND: once you demote it, the agent that kept it can't call it, and is told
           you demoted it, with your note
    """
    # save_skill names a run of its own session, so the run and the keeping share
    # one session.
    #
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {stack.token}"}, timeout=120
        ) as http,
        streamable_http_client(stack.url, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        ran = await session.call_tool("run_python", {"code": TRIED})
        run = ran.content[0].text.rsplit("[run ", 1)[1][:8]  # type: ignore[union-attr]
        kept = await session.call_tool(
            "save_skill", {"name": "doubled", "summary": "Twice a number.", "run": run}
        )
        assert not kept.is_error, kept.content

    # Keeping it asked nothing of you, and a new session calls it at once.
    #
    assert stack.waiting("promote") == []
    called = await stack.text("run_python", code="print(skills.doubled(n=4))")
    assert called.startswith("8\n") and called.endswith("; called skills.doubled]")

    # Asking for it to be promoted puts one item in your queue, and you promote it.
    #
    await stack.text("promote_skill", name="doubled")
    [item] = stack.waiting("promote")
    assert item["title"] == "Promote skills.doubled"
    assert item["risk"] == "medium"
    assert item["diff"]["new"] is True
    assert item["diff"]["lines"][0]["text"] == "def doubled(n: int) -> int:"

    stack.in_control(
        f"i = s.get_item({item['id']!r}); "
        "s.record_decisions([{'item': i['id'], 'content_hash': i['content_hash'], "
        "'action': 'promote'}], {'kind': 'you', 'passkey': 'k'})"
    )
    doc = await stack.text("read_api", name="skills.doubled")
    assert "Promoted: me/local/doubled, version " in doc

    # Demoting it is a decision on top of the promotion, naming it, recorded the same
    # way.
    #
    decided = httpx.get(
        f"{stack.control}/api/v0/items/{item['id']}", headers=stack.bearer
    ).json()
    assert decided["skill"]["id"] == "me/local/doubled"
    assert [a["id"] for a in decided["actions"]] == ["demote"]
    stack.in_control(
        f"i = s.get_item({item['id']!r}); "
        "s.record_decisions([{'item': i['id'], 'content_hash': i['content_hash'], "
        "'action': 'demote', 'supersedes': i['decision']['id'], "
        "'note': 'Use n * 2 inline.'}], {'kind': 'you', 'passkey': 'k'})"
    )
    _, said = await stack.call("run_python", code="print(skills.doubled(n=4))")
    assert "the user demoted skills.doubled, version " in said, said
    assert "Use n * 2 inline. No agent runs that version now" in said
    doc = await stack.text("read_api", name="skills.doubled")
    assert doc.startswith("skills.doubled: the user demoted skills.doubled")


async def test_the_gateway_logs_each_sessions_life_by_short_ids(
    stack: Stack,
) -> None:
    """
    GIVEN: The stack running under compose, and a client that connects, runs a
           script and disconnects
     WHEN: the gateway's log is read
     THEN: it says when the session started, which principal and app it was, its
           name, and when the client ended it, each by the first 8 characters of its
           id
      AND: no whole session id is in the log
    """
    await stack.text("run_python", code="print(1)")
    logs = stack.log("gateway")
    started = re.findall(
        r"Created new transport with session ID: ([0-9a-f]+)$", logs, re.M
    )
    assert started and all(len(s) == 8 for s in started)
    noted = re.search(
        r"session ([0-9a-f]{8}) \(key [0-9a-f]{8}, [a-z]+-[a-z]+-[a-z]+\) of tester, "
        r"app \S+",
        logs,
    )
    assert noted and noted.group(1) in started
    assert re.search(rf"Terminating session: {noted.group(1)}$", logs, re.M)
    assert not re.search(r"[0-9a-f]{32}", logs)


async def test_an_app_on_a_revision_without_sessions_keeps_the_one_it_names(
    stack: Stack,
) -> None:
    """
    GIVEN: The stack running under compose, and a client on MCP's 2026-07-28 revision,
           which has no MCP sessions, naming its session in X-Maji-Session as Claude
           Code's helper does
     WHEN: one script saves a value, a later one loads it, and the gateway's log is
           read
     THEN: the later script gets the value
      AND: the log says the session began, whose it is and which app, by the first 8
           characters of its id, and never holds the whole id
    """
    named = secrets.token_hex(16)
    headers = {"Authorization": f"Bearer {stack.token}", "X-Maji-Session": named}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=120) as http,
        Client(
            streamable_http_client(stack.url, http_client=http),
            mode="2026-07-28",
            client_info=Implementation(name="claude-code", version="2.1.322"),
        ) as client,
    ):
        saved = await client.call_tool("run_python", {"code": "maji.save('n', 42)"})
        loaded = await client.call_tool("run_python", {"code": "print(maji.load('n'))"})
    assert not saved.is_error, saved.content
    assert not loaded.is_error, loaded.content
    assert loaded.content[0].text.split("\n")[0] == "42"  # type: ignore[union-attr]

    logs = stack.log("gateway")
    assert re.search(
        rf"session {named[:8]} \(key [0-9a-f]{{8}}, [a-z]+-[a-z]+-[a-z]+\) of tester, "
        r"app claude-code 2\.1\.322, named by its app$",
        logs,
        re.M,
    )
    assert named not in logs


async def test_run_python_reaches_the_broker_as_the_tokens_principal(
    stack: Stack,
) -> None:
    """
    GIVEN: The stack running under compose, and an MCP client holding a principal's
           token
     WHEN: the client calls run_python with a script that asks who it is
     THEN: the script ran in an executor, reached the broker through the runner's
           registration, and acted as the token's principal
    """
    text = await stack.text("run_python", code="print(maji.whoami()['principal'])")
    output, status = text.rsplit("\n", 1)
    assert output == "tester"
    assert "exit 0" in status


async def test_a_script_in_the_stack_has_no_network(stack: Stack) -> None:
    """
    GIVEN: The stack running under compose
     WHEN: a script run through the gateway tries to reach the internet
     THEN: it fails, and the agent sees the script's error and a nonzero exit
    """
    code = "import socket\nsocket.create_connection(('1.1.1.1', 53), timeout=3)"
    text = await stack.text("run_python", code=code)
    assert "OSError" in text or "gaierror" in text
    assert "exit 1" in text


async def test_the_api_tools_work_through_the_stack(stack: Stack) -> None:
    """
    GIVEN: The stack running under compose, with a policy granting the maji namespace
     WHEN: the client lists and reads the API
     THEN: it sees maji.whoami with its signature
    """
    assert "maji.whoami" in await stack.text("list_api")
    doc = await stack.text("read_api", name="maji.whoami")
    assert doc.startswith("maji.whoami() -> dict")


def test_bring_up_registers_the_first_passkey_with_the_containers_code(
    stack: Stack, owner: Owner
) -> None:
    """
    GIVEN: A new stack, whose control plane has no passkey yet
     WHEN: bring-up asks the control plane's container for a setup code, loads the
           setup page, and registers a passkey with the code, as the page does
     THEN: the page is served with its policy, the passkey is registered with ten
           recovery codes, and the container then refuses to give another code
    """
    with check:
        assert owner.health_before == {
            "ok": True,
            "instance": INSTANCE,
            "setup_needed": True,
        }
    with check:
        page = httpx.get(f"{stack.control}/setup")
        assert "script-src 'self'" in page.headers["content-security-policy"]
    with check:
        assert owner.registered.status_code == 201, owner.registered.text
        assert len(owner.registered.json()["recovery_codes"]) == 10
    with check:
        assert owner.code_again == 3


def test_the_control_plane_answers_only_at_its_name(stack: Stack) -> None:
    """
    GIVEN: The control plane published on the loopback interface
     WHEN: a client reads the queue with the command-line token, at localhost and at
           127.0.0.1
     THEN: the read at localhost succeeds, and the one at the bare address is
           refused, since passkeys are bound to the name
    """
    queue = httpx.get(f"{stack.control}/api/v0/items?lane=ahead", headers=stack.bearer)
    assert queue.status_code == 200
    bare = stack.control.replace("localhost", "127.0.0.1")
    assert httpx.get(f"{bare}/api/v0/health").status_code == 421


def test_the_control_plane_serves_the_web_client(stack: Stack) -> None:
    """
    GIVEN: The service image, with the web client built into it
     WHEN: a browser opens the queue's page and the script it names
     THEN: both are served by the control plane itself, the page with its policy
    """
    page = httpx.get(f"{stack.control}/queue")
    assert page.status_code == 200
    assert '<div id="app">' in page.text
    assert "font-src 'self'" in page.headers["content-security-policy"]
    script = page.text.split('src="', 1)[1].split('"', 1)[0]
    assert httpx.get(f"{stack.control}{script}").status_code == 200


def test_a_backup_from_the_running_control_plane_restores(stack: Stack) -> None:
    """
    GIVEN: The control plane running, with whatever it holds now
     WHEN: a backup is taken from it while it runs, a damaged file is restored, and
           then the backup is restored with the service stopped, as make does
     THEN: the damaged file is refused, and after the real restore the control plane
           answers as the same instance, holding the same passkeys as before
    """
    before = stack.health()
    taken = subprocess.run(
        [*stack.compose, "exec", "-T", "control", "maji-control", "backup"],
        env=stack.env,
        capture_output=True,
    )
    assert taken.returncode == 0, taken.stderr
    assert taken.stdout.startswith(b"SQLite format 3")

    run = [*stack.compose, "run", "--rm", "-T", "--no-deps", "control"]
    subprocess.run([*stack.compose, "stop", "control"], env=stack.env, check=True)
    try:
        damaged = subprocess.run(
            [*run, "maji-control", "restore"],
            env=stack.env,
            input=b"not a database",
            capture_output=True,
        )
        assert damaged.returncode != 0
        restored = subprocess.run(
            [*run, "maji-control", "restore"],
            env=stack.env,
            input=taken.stdout,
            capture_output=True,
        )
        assert restored.returncode == 0, restored.stderr
    finally:
        subprocess.run([*stack.compose, "start", "control"], env=stack.env, check=True)

    assert stack.health() == before


async def test_each_service_stops_when_asked_and_ends_a_script_in_flight(
    stack: Stack,
) -> None:
    """
    GIVEN: The stack running under compose, with a script still running in an
           executor, and an MCP client's session open with the gateway
     WHEN: the runner, the gateway and the broker are each asked to stop, as
           `docker compose stop` asks, with the time Docker allows before it kills a
           container
     THEN: each stops by itself within that time, and none is killed
      AND: the script is ended with the runner, and its executor's container is
           removed, so no script outlives the runner that holds it to its limits
      AND: the agent is told the runner is stopping, and to run the script again
      AND: started again, the stack runs scripts as before
    """
    # A script that would run for two minutes is in flight once it has an executor.
    #
    sleeping = asyncio.create_task(
        stack.call("run_python", code="import time; time.sleep(120)", timeout_s=300)
    )
    async with asyncio.timeout(60):
        while not stack.executors():
            await asyncio.sleep(0.2)

    # The runner is stopped with the script in flight.
    #
    took, exit_code = await asyncio.to_thread(stack.stop, "runner")
    with check:
        assert exit_code != KILLED, "the runner was killed"
    with check:
        assert took < STOP_SECONDS - 1
    with check:
        assert stack.executors() == []
    failed, said = await asyncio.wait_for(sleeping, 60)
    with check:
        assert failed
    with check:
        assert "unavailable: the runner is stopping" in said
    with check:
        assert "run it again" in said

    # The gateway is stopped while a client's session with it is open.
    #
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {stack.token}"}, timeout=120
        ) as http,
        streamable_http_client(stack.url, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        took, exit_code = await asyncio.to_thread(stack.stop, "gateway")
    with check:
        assert exit_code != KILLED, "the gateway was killed"
    with check:
        assert took < STOP_SECONDS - 1

    took, exit_code = await asyncio.to_thread(stack.stop, "broker")
    with check:
        assert exit_code != KILLED, "the broker was killed"
    with check:
        assert took < STOP_SECONDS - 1

    # Started again, the three run a script as before.
    #
    subprocess.run(
        [*stack.compose, "start", "broker", "runner", "gateway"],
        env=stack.env,
        check=True,
        capture_output=True,
    )
    await asyncio.to_thread(stack.wait_for_gateway)
    assert (await stack.text("run_python", code="print(6 * 7)")).startswith("42\n")


async def test_a_runner_killed_outright_removes_its_executor_once_it_runs_again(
    stack: Stack,
) -> None:
    """
    GIVEN: The stack running under compose, with a script still running in an
           executor
     WHEN: the runner is killed outright, as a crash or `kill -9` ends it, and then
           runs again
     THEN: while the runner is gone, the script's executor is still running, with
           nothing to hold it to its deadline
      AND: once the runner runs again, it removes the executor and says so in its
           log, and scripts run as before
    """
    sleeping = asyncio.create_task(
        stack.call("run_python", code="import time; time.sleep(120)", timeout_s=300)
    )
    async with asyncio.timeout(60):
        while not stack.executors():
            await asyncio.sleep(0.2)

    # Killed, the runner can end nothing: the agent's call fails, and the executor
    # is left.
    #
    subprocess.run(
        [*stack.compose, "kill", "runner"],
        env=stack.env,
        check=True,
        capture_output=True,
    )
    failed, _ = await asyncio.wait_for(sleeping, 60)
    assert failed
    assert len(stack.executors()) == 1

    # Running again, the runner removes it before it takes a run of its own.
    #
    subprocess.run(
        [*stack.compose, "start", "runner"],
        env=stack.env,
        check=True,
        capture_output=True,
    )
    async with asyncio.timeout(60):
        while stack.executors():
            await asyncio.sleep(0.2)
    with check:
        assert "runner: removed 1 executor an earlier runner left" in stack.log(
            "runner"
        )

    # The gateway reaches the runner again as soon as the runner is serving.
    #
    async with asyncio.timeout(60):
        while True:
            failed, said = await stack.call("run_python", code="print(6 * 7)")
            if not failed:
                break
            await asyncio.sleep(0.5)
    with check:
        assert said.startswith("42\n")


async def test_a_sentry_key_pasted_in_the_web_app_reaches_only_the_broker(
    stack: Stack, owner: Owner
) -> None:
    """
    GIVEN: The stack running under compose, with its Sentry not connected, and you
           signed in to the control plane with your passkey
     WHEN: a script calls a sentry stub; then you open the Connect item it raised,
           paste a key and your organization's slug, and sign with your passkey; and
           the script runs again
     THEN: the first run is told Sentry isn't connected, naming the item in your queue
      AND: the broker checks the key at the Sentry it is configured with, keeps it,
           and the control plane lists Sentry as connected, by the key's fingerprint
      AND: the script then reads Sentry's projects with the key
      AND: the key is nowhere but the broker's credential store and the requests to
           Sentry: not in the control plane's backup, its answers, the broker's or
           the gateway's log, or what the agent was told
    """
    told: list[str] = []

    # Before you connect it: the script is told, and the item waits for you.
    #
    said = await stack.text("run_python", code="print(sentry.projects())")
    told.append(said)
    with check:
        assert "StubError: unavailable: Sentry isn't connected" in said
    [item] = stack.waiting("connect")
    with check:
        assert item["connection"]["host"] == "host.docker.internal"

    # Connecting: signed in with the passkey, the key goes beside the decision.
    #
    with httpx.Client(base_url=stack.control, headers={"Origin": stack.control}) as you:
        options = you.post("/api/v0/session/options", json={}).json()
        signed_in = you.post(
            "/api/v0/session",
            json={
                "challenge_id": options["challenge_id"],
                "assertion": owner.key.sign(
                    options["public_key"]["challenge"], stack.control
                ),
            },
        )
        assert signed_in.status_code == 204, signed_in.text
        fingerprint = "sha256:" + hashlib.sha256(SENTRY_KEY.encode()).hexdigest()
        choice = {
            "item": item["id"],
            "content_hash": item["content_hash"],
            "action": "connect",
            "credential_fingerprint": fingerprint,
            "credential_settings": {"org": SENTRY_ORG},
        }
        issued = you.post("/api/v0/decisions/challenge", json={"decisions": [choice]})
        assert issued.status_code == 200, issued.text
        challenge = issued.json()
        decided = you.post(
            "/api/v0/decisions",
            json={
                "challenge_id": challenge["challenge_id"],
                "assertion": owner.key.sign(
                    challenge["public_key"]["challenge"], stack.control
                ),
                "secrets": {item["id"]: SENTRY_KEY},
            },
        )
        told.append(decided.text)
        assert decided.status_code == 201, decided.text

        # The broker reads decisions every few seconds.
        #
        deadline = time.monotonic() + 30
        while True:
            listed = you.get("/api/v0/connections").json()["connections"]
            [sentry] = [c for c in listed if c["service"] == "sentry"]
            if sentry["status"] == "connected":
                break
            assert time.monotonic() < deadline, listed
            time.sleep(0.5)
        told.append(json.dumps(listed))
        told.append(you.get(f"/api/v0/items/{item['id']}").text)
    with check:
        assert sentry["fingerprint"] == fingerprint
    with check:
        assert (f"/api/0/organizations/{SENTRY_ORG}/", True) in FakeSentry.seen

    # After: the script reads Sentry.
    #
    ran = await stack.text(
        "run_python", code="print([p['slug'] for p in sentry.projects()])"
    )
    told.append(ran)
    with check:
        assert ran.startswith("['api']\n")

    # The key is nowhere else.
    #
    backup = subprocess.run(
        [*stack.compose, "exec", "-T", "control", "maji-control", "backup"],
        env=stack.env,
        capture_output=True,
    )
    with check:
        assert SENTRY_KEY.encode() not in backup.stdout
    with check:
        assert SENTRY_KEY not in stack.log("broker") + stack.log("gateway")
    with check:
        assert not [t for t in told if SENTRY_KEY in t]
