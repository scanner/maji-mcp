"""
Tests for `maji-setup`, which creates a compose stack's secret files, opens the setup
page, and connects Claude Code and Claude Desktop to the gateway.
"""

# system imports
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.host import setup


@pytest.fixture
def secrets(tmp_path: Path) -> Path:
    """Where `maji-setup init` is to make the secrets directory, which isn't there yet."""
    return tmp_path / "secrets"


@pytest.fixture
def token_hashes(secrets: Path) -> Callable[[], dict[str, str]]:
    """
    A function that reads each principal's token hash from gateway_tokens.json, as the
    file is when it is called.
    """

    def read() -> dict[str, str]:
        hashes: dict[str, str] = json.loads(
            (secrets / "gateway_tokens.json").read_text()
        )["principals"]
        return hashes

    return read


@pytest.fixture
def hash_of() -> Callable[[Path], str]:
    """A function that returns the SHA-256 of the token a file holds, in hex."""

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_text().encode()).hexdigest()

    return digest


def test_init_creates_every_secret_readable_only_by_you(
    secrets: Path,
    token_hashes: Callable[[], dict[str, str]],
    hash_of: Callable[[Path], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    GIVEN: No secrets directory, and Sentry's settings in the environment
     WHEN: maji-setup init runs for a principal
     THEN: the directory is yours alone, and it holds a gateway token for the
           principal with only its hash in gateway_tokens.json, the bridge's token,
           the instance id from the host's name, and a control plane client token
           with only its hash in control_clients.json, each readable only by you
      AND: no key for a service the scripts call, such as Sentry's, is written:
           you paste those into the web app
    """
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "sntryu_" + "0" * 64)
    monkeypatch.setenv("SENTRY_ORG", "acme")
    monkeypatch.setattr(setup.socket, "gethostname", lambda: "Studio-Mac.example.net")
    setup.init(secrets, "claude-code")

    # The directory is yours alone, and so is each file in it.
    #
    with check:
        assert secrets.stat().st_mode & 0o777 == 0o700
    with check:
        assert sorted(p.name for p in secrets.iterdir()) == [
            "claude-code.token",
            "control-cli.token",
            "control_clients.json",
            "emacs-org.token",
            "gateway_tokens.json",
            "instance",
        ]
    for path in secrets.iterdir():
        with check:
            assert path.stat().st_mode & 0o777 == 0o600, path.name

    # Each token is in a file of its own, with only its hash where a service reads
    # it.
    #
    with check:
        assert token_hashes() == {"claude-code": hash_of(secrets / "claude-code.token")}
    with check:
        clients = json.loads((secrets / "control_clients.json").read_text())
        assert clients["clients"] == {
            "cli": "sha256:" + hash_of(secrets / "control-cli.token")
        }

    # The instance is named for the host.
    #
    with check:
        assert (secrets / "instance").read_text() == "studio-mac\n"


def test_init_keeps_what_exists(
    secrets: Path, faker: Faker, new_token: Callable[[], str]
) -> None:
    """
    GIVEN: A secrets directory set up before, holding a principal's token hash but not
           the token itself, as a hand-made setup does
     WHEN: maji-setup init runs again for that principal
     THEN: every existing file is left exactly as it was, and only the missing ones
           are created
    """
    hashes = json.dumps({"principals": {"me": faker.sha256()}})
    bridge_token = new_token()
    secrets.mkdir(mode=0o700)
    (secrets / "gateway_tokens.json").write_text(hashes)
    (secrets / "emacs-org.token").write_text(bridge_token)

    report = setup.init(secrets, "me")

    with check:
        assert (secrets / "gateway_tokens.json").read_text() == hashes
    with check:
        assert (secrets / "emacs-org.token").read_text() == bridge_token
    with check:
        assert not (secrets / "me.token").exists()
    with check:
        assert (secrets / "instance").exists()
    with check:
        assert any(line.startswith("kept     gateway_tokens.json") for line in report)


def test_init_adds_a_new_principal_beside_the_old(
    secrets: Path,
    token_hashes: Callable[[], dict[str, str]],
    hash_of: Callable[[Path], str],
) -> None:
    """
    GIVEN: A secrets directory with one principal
     WHEN: maji-setup init runs for another
     THEN: the new principal gets a token, and the first one's hash is untouched
    """
    setup.init(secrets, "first")
    before = token_hashes()
    setup.init(secrets, "second")
    after = token_hashes()
    with check:
        assert after["first"] == before["first"]
    with check:
        assert after["second"] == hash_of(secrets / "second.token")


def test_token_replaces_one_principals_token(
    secrets: Path,
    token_hashes: Callable[[], dict[str, str]],
    hash_of: Callable[[Path], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    GIVEN: A secrets directory with two principals
     WHEN: maji-setup token runs for one of them
     THEN: that principal's token file and hash change together, the other's hash is
           kept, and the new token is never printed
    """
    setup.init(secrets, "me")
    setup.init(secrets, "other")
    old_token = (secrets / "me.token").read_text()
    other = token_hashes()["other"]

    monkeypatch.setattr(
        sys, "argv", ["maji-setup", "--secrets", str(secrets), "token", "me"]
    )
    setup.main()

    rotated = (secrets / "me.token").read_text()
    printed = capsys.readouterr()
    with check:
        assert rotated != old_token
    with check:
        assert token_hashes() == {"me": hash_of(secrets / "me.token"), "other": other}
    with check:
        assert (secrets / "me.token").stat().st_mode & 0o777 == 0o600
    with check:
        assert rotated not in printed.out + printed.err


# Names a principal can't have: each would name a file other than a token file of
# the principal's own in the secrets directory.
#
NOT_A_PRINCIPALS_NAME = [
    pytest.param("../escaped", id="a path out of the directory"),
    pytest.param("sub/name", id="a path into a folder"),
    pytest.param("", id="empty"),
    pytest.param(".hidden", id="starting with a dot"),
    pytest.param("-flag", id="starting with a hyphen"),
    pytest.param("two words", id="holding a space"),
    pytest.param("n" * 65, id="longer than 64 characters"),
    pytest.param("emacs-org", id="the name of the bridge's token file"),
    pytest.param("control-cli", id="the name of the command-line client's"),
]


@pytest.mark.parametrize("name", NOT_A_PRINCIPALS_NAME)
def test_a_name_that_cant_be_a_principals_gets_no_token(
    secrets: Path, tmp_path: Path, name: str
) -> None:
    """
    GIVEN: A secrets directory set up for one principal
     WHEN: maji-setup is asked for a token for a principal whose name isn't letters,
           digits, hyphens and underscores, or is the name of a token file setup
           keeps for the bridge or the control plane's command-line client
     THEN: it is refused, saying what a name may be
      AND: no file is written or changed, in the secrets directory or beside it, so
           a name never reaches a file that isn't the principal's own token
    """
    setup.init(secrets, "claude-code")

    def files() -> dict[Path, bytes]:
        return {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    before = files()

    with pytest.raises(ValueError, match="can't be a principal's name"):
        setup.rotate(secrets, name)
    assert files() == before


@pytest.mark.parametrize("name", NOT_A_PRINCIPALS_NAME)
def test_init_for_a_name_that_cant_be_a_principals_creates_nothing(
    secrets: Path, name: str
) -> None:
    """
    GIVEN: No secrets directory
     WHEN: maji-setup init runs for a principal whose name can't be one
     THEN: it is refused before anything is created, the directory included
    """
    with pytest.raises(ValueError, match="can't be a principal's name"):
        setup.init(secrets, name)
    assert not secrets.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["token", "../escaped"],
        ["init", "--principal", "../escaped"],
        ["claude-code-json", "--helper", "bin/maji-headers", "--principal", "a/b"],
        ["connect-claude-desktop", "--config", "c.json", "--principal", "a/b"],
    ],
    ids=["token", "init", "claude-code-json", "connect-claude-desktop"],
)
def test_every_command_refuses_such_a_name_as_a_usage_error(
    secrets: Path, tmp_path: Path, arguments: list[str]
) -> None:
    """
    GIVEN: The maji-setup command
     WHEN: any of its commands is given a principal whose name can't be one
     THEN: it exits as a usage error that says what a name may be, and creates
           nothing
    """
    ran = subprocess.run(
        [sys.executable, "-m", "maji_mcp.host.setup", "--secrets", str(secrets)]
        + arguments,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=30,
    )

    with check:
        assert ran.returncode == 2
    with check:
        assert "can't be a principal's name: a name is 1 to 64 letters" in ran.stderr
    with check:
        assert list(tmp_path.iterdir()) == []


###############################################################################
#
@pytest.mark.parametrize(
    "terminal", [True, False], ids=["from a terminal", "from a script"]
)
def test_the_setup_page_opens_only_while_a_passkey_is_needed(
    monkeypatch: pytest.MonkeyPatch, faker: Faker, terminal: bool
) -> None:
    """
    GIVEN: A control plane with no passkey, and make up run from a terminal or not
     WHEN: maji-setup setup-page runs
     THEN: If there is a terminal, it opens the setup page with the container's code
           in the link's fragment
      AND: If there isn't, as when a script runs make up, it prints the link instead
    """
    code = faker.password(length=8, special_chars=False)
    ran: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        """Note a command in place of running it, answering `docker` with the code."""
        ran.append(command)
        said = f"{code}\n" if command[0] == "docker" else ""
        return subprocess.CompletedProcess(command, 0, stdout=said, stderr="")

    monkeypatch.setattr(setup, "wait_for_control", lambda port: {"setup_needed": True})
    monkeypatch.setattr(setup.subprocess, "run", run)
    monkeypatch.setattr(setup.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(setup.sys.stdout, "isatty", lambda: terminal)

    said = setup.setup_page(8767)

    link = f"http://localhost:8767/setup#code={code}"
    assert ran[0][-2:] == ["maji-control", "setup-code"]
    if terminal:
        assert ran[1] == ["/usr/bin/open", link]
        assert "opened the setup page" in said
    else:
        assert len(ran) == 1
        assert link in said


def test_the_setup_page_stays_shut_once_a_passkey_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    GIVEN: A control plane that has a passkey
     WHEN: make up runs maji-setup setup-page
     THEN: it asks the container for no code, opens nothing, and says so
    """
    monkeypatch.setattr(setup, "wait_for_control", lambda port: {"setup_needed": False})
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: pytest.fail("ran"))
    assert "nothing to set up" in setup.setup_page(8767)


###############################################################################
#
@pytest.fixture
def token_dir(tmp_path: Path, new_token: Callable[[], str]) -> Path:
    """
    A secrets directory, under a path with a space in it, holding a made-up token for
    claude-code in `claude-code.token`.
    """
    directory = tmp_path / "my secrets"
    directory.mkdir()
    (directory / "claude-code.token").write_text(new_token())
    return directory


def test_claude_codes_entry_runs_the_helper_on_the_token_file(
    token_dir: Path, tmp_path: Path
) -> None:
    """
    GIVEN: A secrets directory holding a principal's token file, under a path with a
           space in it
     WHEN: Claude Code's entry for the gateway is made, as `make connect-claude-code`
           gives it to `claude mcp add-json`
     THEN: it is an HTTP server at the gateway's port whose headers come from the
           helper, run on the token file, both by absolute path quoted for the shell;
           the token itself is nowhere in it
      AND: a principal with no token file has no entry, nor does a name that can't
           be a principal's
    """
    helper = tmp_path / "bin" / "maji-headers"

    entry = setup.claude_entry(token_dir, "claude-code", 8765, helper)

    with check:
        assert entry == {
            "type": "http",
            "url": "http://127.0.0.1:8765/mcp",
            "headersHelper": f"{helper} '{token_dir}/claude-code.token'",
        }
    with check:
        assert (token_dir / "claude-code.token").read_text() not in json.dumps(entry)
    with check, pytest.raises(FileNotFoundError):
        setup.claude_entry(token_dir, "codex", 8765, helper)
    with check, pytest.raises(ValueError, match="can't be a principal's name"):
        setup.claude_entry(token_dir, "../claude-code", 8765, helper)


def test_claude_desktops_entry_starts_the_relay_on_the_token_file(
    token_dir: Path, tmp_path: Path
) -> None:
    """
    GIVEN: A principal's token file, and the relay installed
     WHEN: Claude Desktop's entry for the gateway is made
     THEN: Desktop starts the relay by its absolute path, given the token file's
           absolute path and the gateway's address; the token itself is nowhere in it
      AND: a principal with no token file, a name that can't be a principal's, or a
           relay not installed, has no entry
    """
    relay = tmp_path / "bin" / "maji-relay"
    relay.parent.mkdir()
    relay.write_text("")

    entry = setup.desktop_entry(token_dir, "claude-code", 8765, relay)

    with check:
        assert entry == {
            "command": str(relay),
            "args": [
                "--token-file",
                f"{token_dir}/claude-code.token",
                "--url",
                "http://127.0.0.1:8765/mcp",
            ],
        }
    with check:
        assert (token_dir / "claude-code.token").read_text() not in json.dumps(entry)
    with check, pytest.raises(FileNotFoundError):
        setup.desktop_entry(token_dir, "codex", 8765, relay)
    with check, pytest.raises(ValueError, match="can't be a principal's name"):
        setup.desktop_entry(token_dir, "../claude-code", 8765, relay)
    with check, pytest.raises(FileNotFoundError, match="uv sync"):
        setup.desktop_entry(token_dir, "claude-code", 8765, tmp_path / "missing")


###############################################################################
#
@pytest.fixture
def config(tmp_path: Path) -> Path:
    """Where Claude Desktop keeps its settings, in a folder that exists; no file yet."""
    return tmp_path / "claude_desktop_config.json"


@pytest.fixture
def entry(faker: Faker) -> dict[str, Any]:
    """Claude Desktop's entry for the relay, with a made-up command and token file."""
    return {
        "command": faker.file_path(depth=3, extension=""),
        "args": ["--token-file", faker.file_path(depth=2, extension="token")],
    }


@pytest.mark.parametrize(
    ("before", "replace_existing"),
    [
        (
            {
                "mcpServers": {
                    "emacs-org": {"command": "uv", "args": ["run", "server.py"]}
                },
                "preferences": {"sidebarMode": "chat"},
                "coworkUserFilesPath": "/somewhere",
            },
            False,
        ),
        (
            {
                "theme": "dark",
                "mcpServers": {
                    "maji": {
                        "command": "/old/checkout/.venv/bin/maji-relay",
                        "args": ["--token-file"],
                    },
                    "other": {"command": "other-server"},
                },
            },
            True,
        ),
    ],
    ids=["beside another server", "in place of a different maji, when asked"],
)
def test_maji_is_added_to_claude_desktops_settings_and_all_else_kept(
    config: Path,
    entry: dict[str, Any],
    before: dict[str, Any],
    replace_existing: bool,
) -> None:
    """
    GIVEN: Claude Desktop's settings, with another server and settings of its own,
           and either no `maji` server, or one that names a relay and a token file
           that have since moved
     WHEN: maji is added, with `replace_existing` where there is a `maji` already, as
           `make connect-claude-desktop REPLACE=1` adds it, and then added again
     THEN: the file gains maji beside the other server, in the old one's place if
           there was one, with every other server and setting as it was, readable
           only by you
      AND: adding it again finds it there and leaves the file as it is
    """
    config.write_text(json.dumps(before))

    added = setup.connect_desktop(config, entry, replace_existing=replace_existing)
    assert added is True
    written = config.read_text()
    with check:
        assert json.loads(written) == {
            **before,
            "mcpServers": {**before["mcpServers"], "maji": entry},
        }
    with check:
        assert config.stat().st_mode & 0o777 == 0o600

    assert setup.connect_desktop(config, entry) is False
    assert config.read_text() == written


def test_maji_is_added_to_settings_claude_desktop_hasnt_written_yet(
    config: Path, entry: dict[str, Any], tmp_path: Path
) -> None:
    """
    GIVEN: Claude Desktop installed, with no settings file yet
     WHEN: maji is added
     THEN: the file is made, holding maji alone
      AND: with no Claude Desktop folder at all, nothing is made
    """
    assert setup.connect_desktop(config, entry) is True
    assert json.loads(config.read_text()) == {"mcpServers": {"maji": entry}}

    with pytest.raises(FileNotFoundError, match="is Claude Desktop installed"):
        setup.connect_desktop(tmp_path / "Claude" / "config.json", entry)
    assert not (tmp_path / "Claude").exists()


@pytest.mark.parametrize(
    ("content", "refusal"),
    [
        (
            '{"mcpServers": {"maji": {"command": "npx"}}}',
            "already has a different server",
        ),
        ('{"mcpServers": {"x": 1}, ', "isn't JSON"),
        ("[1, 2]", "isn't a JSON object"),
        ('{"mcpServers": []}', "mcpServers isn't an object"),
    ],
)
def test_settings_maji_cant_join_safely_are_left_as_they_are(
    config: Path, entry: dict[str, Any], content: str, refusal: str
) -> None:
    """
    GIVEN: Claude Desktop settings with a different server already named maji, or
           that can't be read as settings
     WHEN: maji is added
     THEN: it is refused, saying why, and the file is left exactly as it was
    """
    config.write_text(content)
    with pytest.raises(ValueError, match=refusal):
        setup.connect_desktop(config, entry)
    assert config.read_text() == content


def test_a_settings_file_that_is_a_link_is_left_as_it_is(
    config: Path, entry: dict[str, Any], tmp_path: Path
) -> None:
    """
    GIVEN: Claude Desktop's settings file is a link to a file elsewhere
     WHEN: maji is added
     THEN: it is refused, so the link isn't replaced by a file of its own
    """
    target = tmp_path / "dotfiles.json"
    target.write_text("{}")
    config.symlink_to(target)
    with pytest.raises(ValueError, match="is a link"):
        setup.connect_desktop(config, entry)
    assert config.is_symlink() and target.read_text() == "{}"
