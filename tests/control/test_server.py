"""
Tests for the control plane service's commands (`server.py`): the setup code it
prints, backup and restore, reading the instance id and client tokens it starts
with, and stopping when asked.
"""

# system imports
import http.client
import io
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

# 3rd party imports
import pytest
from faker import Faker

# project imports
from maji_mcp.control.demo import DEMO
from maji_mcp.control.server import (
    backup,
    read_clients,
    read_instance,
    restore,
    setup_code,
)
from maji_mcp.control.store import ControlStore
from tests.control.factories import QuestionFactory
from tests.support.contract import Authenticator
from tests.support.control import INSTANCE, Control


async def test_the_code_maji_control_prints_registers_the_first_passkey(
    control: Control, store: ControlStore
) -> None:
    """
    GIVEN: A new instance, and the code `maji-control setup-code` prints for it
     WHEN: the setup page registers a passkey with that code, then the command is run
           again
     THEN: the passkey is registered, and the command then has no code to give
    """
    code = setup_code(store)
    assert code is not None

    made = await control.register(Authenticator(), "this Mac", setup_code=code)

    assert made.status_code == 201
    assert setup_code(store) is None


@pytest.fixture
def backup_of(database: Callable[..., tuple[Path, str]]) -> Callable[[str], bytes]:
    """
    A function that makes a second database, for the instance named, and returns a
    backup of it.
    """

    def backup_of(instance: str) -> bytes:
        other, _ = database(instance, "other.db")
        copy = io.BytesIO()
        backup(other, copy)
        return copy.getvalue()

    return backup_of


def test_a_backup_restores_the_queue(
    database: Callable[..., tuple[Path, str]],
) -> None:
    """
    GIVEN: A control plane database with an item in it, and a backup of it
     WHEN: the backup is restored over the database, which has queued another item
           since
     THEN: the queue is as it was backed up, and the restored database opens as the
           same instance
    """
    live, item = database()
    copy = io.BytesIO()
    backup(live, copy)
    since = ControlStore(live, INSTANCE)
    since.create_item(**QuestionFactory.build())
    since.db.close()

    restore(live, INSTANCE, copy.getvalue())

    restored = ControlStore(live, INSTANCE)
    assert [i["id"] for i in restored.list_items("ahead").rows] == [item]
    restored.db.close()


@pytest.mark.parametrize(
    ("backed_up", "refusal"),
    [
        (lambda backup_of: b"not a database", "isn't a sound"),
        (lambda backup_of: backup_of("laptop"), "belongs to laptop"),
    ],
    ids=["damaged", "another instance"],
)
def test_a_restore_that_doesnt_fit_leaves_the_database_alone(
    tmp_path: Path,
    database: Callable[..., tuple[Path, str]],
    backup_of: Callable[[str], bytes],
    backed_up: Callable[[Callable[[str], bytes]], bytes],
    refusal: str,
) -> None:
    """
    GIVEN: A control plane database, and a backup that is damaged or was taken from
           another instance
     WHEN: it is restored
     THEN: it is refused, and the database keeps what it had
    """
    live, item = database()

    with pytest.raises(SystemExit, match=refusal):
        restore(live, INSTANCE, backed_up(backup_of))

    kept = ControlStore(live, INSTANCE)
    assert [i["id"] for i in kept.list_items("ahead").rows] == [item]
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".restore-")] == []
    kept.db.close()


def test_the_instance_id_must_be_one_label(tmp_path: Path) -> None:
    """
    GIVEN: An instance file holding the host's name as recorded, or something else
     WHEN: the control plane starts with it
     THEN: a DNS label is accepted, and anything else stops it from starting
    """
    good, bad = tmp_path / "good", tmp_path / "bad"
    good.write_text("studio-mac\n")
    bad.write_text("Studio Mac\n")
    assert read_instance(good) == "studio-mac"
    with pytest.raises(SystemExit):
        read_instance(bad)


def test_client_tokens_come_as_hashes(tmp_path: Path) -> None:
    """
    GIVEN: The clients file maji-setup writes, and a stack without one
     WHEN: the control plane reads it
     THEN: it gets each client's name and token hash, or no clients at all
    """
    clients = tmp_path / "control_clients.json"
    clients.write_text('{"clients": {"cli": "sha256:abc"}}')
    assert read_clients(clients) == {"cli": "sha256:abc"}
    assert read_clients(tmp_path / "missing.json") == {}


def test_it_stops_on_ctrl_c_with_an_event_stream_open(
    tmp_path: Path, unused_tcp_port: int, faker: Faker
) -> None:
    """
    GIVEN: The demo control plane, with a signed-in browser following its event
           stream, as an open queue tab does
     WHEN: it is sent SIGINT, as Ctrl-C does
     THEN: the stream ends and the control plane exits within a few seconds,
           quietly, with the status a shell expects after Ctrl-C
    """
    port = unused_tcp_port
    session = faker.sha256()
    server = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from maji_mcp.control.server import main; main()",
            "demo",
            "--dir",
            str(tmp_path),
            "--port",
            str(port),
            "--web-dir",
            str(tmp_path / "web"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        # The control plane is up once its port answers.
        #
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)

        # A browser follows the event stream. Its session is made straight in the
        # demo's database, standing in for the passkey sign-in the stream requires.
        #
        signed_in = ControlStore(tmp_path / "control.db", DEMO)
        signed_in.start_session(session, "pk")
        signed_in.db.close()
        browser = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        browser.request(
            "GET",
            "/api/v0/events",
            headers={"Host": f"localhost:{port}", "Cookie": f"maji_session={session}"},
        )
        assert browser.getresponse().status == 200

        # Ctrl-C ends it promptly, as a shell expects, without a traceback.
        #
        asked = time.monotonic()
        server.send_signal(signal.SIGINT)
        output, _ = server.communicate(timeout=10)
        assert time.monotonic() - asked < 5
        assert server.returncode == 130
        assert b"Traceback" not in output
    finally:
        server.kill()
        server.wait()
