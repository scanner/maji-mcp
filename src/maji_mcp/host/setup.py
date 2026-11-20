"""
Create the files a compose stack reads its secrets from, without overwriting any.

`compose.yaml` mounts each secret from a file in the secrets directory
(`~/.config/maji-mcp/secrets`, or `MAJI_SECRETS`). `maji-setup init` creates the
missing ones:

- `gateway_tokens.json`: the hash of each principal's gateway token. A principal not in
  it gets a new token, written beside it as `<principal>.token` for
  `make connect-claude-code`.
- `emacs-org.token`: the token the bridge in front of emacs-org-mcp asks for, so the
  bridge (`make bridge`) and the broker read the same file
- `instance`: this instance's id, from the host's name, recorded once because it goes
  into signed hashes (`api/openapi.yaml`, `InstanceIdentifier`)
- `control_clients.json`: the hash of each control plane client's token, with
  `control-cli.token` for a command-line client. A token reads the queue and narrows
  access; it never decides.

Keys for the services scripts call, such as Sentry's, are none of these: you paste
each into the web app, and only the broker keeps it (`broker/connections.py`).

A file that exists is kept as it is. `maji-setup token <principal>` replaces one
principal's token, for a lost token or a new client; the gateway reads the hashes at
start, so it needs a restart after.

A principal's name is also its token file's name, so a name is letters, digits,
hyphens and underscores (`checked_principal`). Every command takes a name through
that check before it touches a file.

The directory is created readable only by you, and each file is created mode 600 in
the same call that opens it. No token is ever printed.

`maji-setup setup-page` runs after the stack starts. While no passkey is registered,
it gets a one-time code from the control plane and opens the setup page with the code
in the link's fragment, which a browser never sends on; with no terminal, as when a
script runs `make up`, it prints the link instead.

`maji-setup claude-code-json` prints Claude Code's entry for the gateway, which
`make connect-claude-code` gives `claude mcp add-json`: the gateway's address, and a
helper that reads the token from its file each time Claude Code connects
(`claude_entry`).

`maji-setup connect-claude-desktop` adds the gateway to Claude Desktop's
configuration, which `make connect-claude-desktop` runs with Desktop closed: the relay
(`maji-relay`) as the command Desktop starts, with the token file it reads
(`desktop_entry`).

    uv run maji-setup init --principal claude-code
    uv run maji-setup token claude-code
    uv run maji-setup setup-page --port 8767
    uv run maji-setup claude-code-json --helper bin/maji-headers
    uv run maji-setup connect-claude-desktop --config <claude_desktop_config.json>
"""

# system imports
import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

# project imports
from maji_mcp.control.ids import instance_id

# Where maji-mcp keeps what belongs to you on this machine. The directory is hidden at
# the top of your home directory, so the runner never mounts it into an executor
# (`runner/mounts.py`), and a project that uses maji-mcp holds none of its secrets.
#
CONFIG_DIR = Path.home() / ".config" / "maji-mcp"
SECRETS_DIR = CONFIG_DIR / "secrets"

# What a principal's name may be. The name is also the name of the principal's token
# file, `<principal>.token`, so it holds nothing that could name a file anywhere but
# in the secrets directory.
#
PRINCIPAL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

# The token files setup keeps for something other than a principal, by the name a
# principal would have to take to share one, and whose each is.
#
KEPT_TOKEN_FILES = {
    "emacs-org": "the bridge",
    "control-cli": "the control plane's command-line client",
}


###############################################################################
#
def write_new(path: Path, content: str) -> None:
    """
    Create a file readable only by you, failing if it already exists.

    Raises:
        FileExistsError: the file exists
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(content)


def replace(path: Path, content: str) -> None:
    """Replace a file's content atomically, keeping it readable only by you."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    write_new(tmp, content)
    tmp.replace(path)


def checked_principal(name: str) -> str:
    """
    Return a principal's name, once it is known to name only the principal's own
    token file.

    Raises:
        ValueError: the name isn't 1 to 64 letters, digits, hyphens and underscores
            starting with a letter or a digit, or its token file is one setup keeps
            for the bridge or for the control plane's command-line client
    """
    if not PRINCIPAL_NAME.fullmatch(name):
        raise ValueError(
            f"{name!r} can't be a principal's name: a name is 1 to 64 letters, "
            "digits, hyphens and underscores, and starts with a letter or a digit"
        )
    if name in KEPT_TOKEN_FILES:
        raise ValueError(
            f"{name!r} can't be a principal's name: {name}.token is the token of "
            f"{KEPT_TOKEN_FILES[name]}"
        )
    return name


def token_file(directory: Path, principal: str) -> Path:
    """
    Return where a principal's gateway token is kept: `<principal>.token` in the
    secrets directory.

    Raises:
        ValueError: the principal's name can't name its token file
            (`checked_principal`)
    """
    return directory / f"{checked_principal(principal)}.token"


def mint(directory: Path, principal: str) -> str:
    """
    Write a new token for a principal to `<principal>.token`, and return its hash.

    An existing token file is replaced: this is the rotation.

    Raises:
        ValueError: the principal's name can't name its token file
    """
    path = token_file(directory, principal)
    token = secrets.token_urlsafe(32)
    replace(path, token)
    return hashlib.sha256(token.encode()).hexdigest()


def read_hashes(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    principals: dict[str, str] = json.loads(path.read_text())["principals"]
    return principals


def write_hashes(path: Path, principals: dict[str, str]) -> None:
    replace(path, json.dumps({"principals": principals}, indent=2) + "\n")


###############################################################################
#
def init(directory: Path, principal: str) -> list[str]:
    """
    Create every missing secret file, and return a line describing each one.

    Returns:
        One line per file: `created` or `kept`, and where a value came from. Values
        themselves never appear.

    Raises:
        ValueError: the principal's name can't name its token file. Nothing is
            created, the directory included.
    """
    checked_principal(principal)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    report = []

    tokens = directory / "gateway_tokens.json"
    principals = read_hashes(tokens)
    if principal in principals:
        report.append(f"kept     {tokens.name} ({principal} has a token)")
    else:
        principals[principal] = mint(directory, principal)
        write_hashes(tokens, principals)
        report.append(
            f"created  {tokens.name} and {principal}.token (a new token for {principal})"
        )

    bridge = directory / "emacs-org.token"
    if bridge.exists():
        report.append(f"kept     {bridge.name}")
    else:
        write_new(bridge, secrets.token_urlsafe(32))
        report.append(f"created  {bridge.name} (for make bridge)")

    instance = directory / "instance"
    if instance.exists():
        report.append(f"kept     {instance.name} ({instance.read_text().strip()})")
    else:
        made = instance_id(socket.gethostname())
        write_new(instance, made + "\n")
        report.append(f"created  {instance.name} ({made}, from this host's name)")

    clients = directory / "control_clients.json"
    if clients.exists():
        report.append(f"kept     {clients.name}")
    else:
        token = secrets.token_urlsafe(32)
        write_new(directory / "control-cli.token", token)
        digest = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
        write_new(clients, json.dumps({"clients": {"cli": digest}}, indent=2) + "\n")
        report.append(f"created  {clients.name} and control-cli.token (reads only)")
    return report


def wait_for_control(port: int, seconds: float = 60.0) -> dict[str, object]:
    """Wait for the control plane to answer its health check, and return the answer."""
    deadline = time.monotonic() + seconds
    url = f"http://localhost:{port}/api/v0/health"
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                answer: dict[str, object] = json.loads(response.read())
                return answer
        except OSError:
            if time.monotonic() > deadline:
                raise SystemExit(
                    f"setup: the control plane didn't answer at {url}"
                ) from None
            time.sleep(1)


def setup_page(port: int) -> str:
    """
    Open the setup page for the first passkey, or say there's nothing to set up.

    The code comes from `maji-control setup-code` inside the control plane's
    container, and goes into the link's fragment, which a browser never sends on.

    Returns:
        A line saying what happened
    """
    if not wait_for_control(port).get("setup_needed"):
        return "the control plane has a passkey; nothing to set up"
    made = subprocess.run(
        ["docker", "compose", "exec", "-T", "control", "maji-control", "setup-code"],
        capture_output=True,
        text=True,
        check=False,
    )
    code = made.stdout.strip()
    if made.returncode != 0 or not code:
        return f"no setup code: {made.stderr.strip() or 'maji-control gave none'}"
    link = f"http://localhost:{port}/setup#code={code}"
    opener = shutil.which("open") or shutil.which("xdg-open")
    if sys.stdout.isatty() and opener:
        subprocess.run([opener, link], check=False)
        return "opened the setup page; create your passkey there (the link works once)"
    return f"create your passkey at {link} (it works once, for ten minutes)"


def claude_entry(
    directory: Path, principal: str, port: int, helper: Path
) -> dict[str, str]:
    """
    Return Claude Code's entry for the gateway, as `claude mcp add-json` takes it.

    Claude Code runs the helper (`bin/maji-headers`) each time it connects. The
    helper reads the principal's token from its file, so the token stays in the
    secrets directory instead of being copied into Claude Code's configuration, and
    names a new session for the gateway. Claude Code runs a user-scope helper from
    its own configuration directory, so both paths are absolute.

    Raises:
        FileNotFoundError: the principal has no token file, as when its token was
            made by hand
        ValueError: the principal's name can't name its token file
    """
    token = token_file(directory, principal).resolve()
    if not token.is_file():
        raise FileNotFoundError(f"no {token}")
    return {
        "type": "http",
        "url": f"http://127.0.0.1:{port}/mcp",
        "headersHelper": shlex.join([str(helper.resolve()), str(token)]),
    }


def desktop_entry(
    directory: Path, principal: str, port: int, relay: Path
) -> dict[str, Any]:
    """
    Return Claude Desktop's entry for the gateway: the relay, started as a command.

    Claude Desktop starts the relay (`maji-relay`) when it opens, and the relay reads
    the principal's token from its file for each request, so the entry names the file
    and holds no token. Desktop starts it without this project's environment or a
    shell's PATH, so both paths are absolute.

    Raises:
        FileNotFoundError: the principal has no token file, or the relay isn't
            installed beside this Python
        ValueError: the principal's name can't name its token file
    """
    token = token_file(directory, principal).resolve()
    if not token.is_file():
        raise FileNotFoundError(f"no {token}")
    if not relay.is_file():
        raise FileNotFoundError(f"no {relay}: run `uv sync` to install maji-relay")
    return {
        "command": str(relay),
        "args": ["--token-file", str(token), "--url", f"http://127.0.0.1:{port}/mcp"],
    }


def connect_desktop(
    config: Path, entry: dict[str, Any], *, replace_existing: bool = False
) -> bool:
    """
    Add the gateway to Claude Desktop's configuration as `maji`, keeping all else.

    Desktop keeps its own settings in the same file, so every other key and server is
    written back as it was read, and a file that can't be read as settings is left
    alone.

    Args:
        config: Claude Desktop's settings file
        entry: the server entry to add (`desktop_entry`)
        replace_existing: put this entry in place of a different server named
            `maji`, as after the secrets directory or the checkout has moved, when
            the entry there names paths that are gone

    Returns:
        True if `maji` was added, False if the same entry was there already

    Raises:
        FileNotFoundError: the file's folder doesn't exist, as when Claude Desktop
            isn't installed
        ValueError: the file isn't a JSON object of settings, is a link, or already
            has a different server named `maji` that `replace_existing` doesn't
            cover
    """
    if not config.parent.is_dir():
        raise FileNotFoundError(f"no {config.parent}: is Claude Desktop installed?")
    if config.is_symlink():
        raise ValueError(f"{config} is a link: add maji to the file it points to")
    settings: Any = {}
    if config.exists():
        try:
            settings = json.loads(config.read_text())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"{config} isn't JSON ({exc}); it was left as it is"
            ) from exc
    if not isinstance(settings, dict):
        raise ValueError(f"{config} isn't a JSON object; it was left as it is")
    servers = settings.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{config}'s mcpServers isn't an object; it was left as it is")
    existing = servers.get("maji")
    if existing == entry:
        return False
    if existing is not None and not replace_existing:
        raise ValueError(
            f"{config} already has a different server named maji; "
            "`make connect-claude-desktop REPLACE=1` puts this one in its place"
        )
    servers["maji"] = entry
    replace(config, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    return True


def rotate(directory: Path, principal: str) -> None:
    """
    Give a principal a new gateway token, replacing its old one.

    Raises:
        ValueError: the principal's name can't name its token file. Nothing is
            written.
    """
    tokens = directory / "gateway_tokens.json"
    principals = read_hashes(tokens)
    principals[principal] = mint(directory, principal)
    write_hashes(tokens, principals)


###############################################################################
#
def principal_argument(value: str) -> str:
    """
    Check a principal's name as argparse reads it, so a name that can't be one is a
    usage error before any command runs.

    Raises:
        argparse.ArgumentTypeError: saying what a name may be
    """
    try:
        return checked_principal(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="create a compose stack's secrets")
    parser.add_argument(
        "--secrets",
        type=Path,
        default=Path(os.environ.get("MAJI_SECRETS") or SECRETS_DIR),
        help=f"the secrets directory (default: $MAJI_SECRETS, or {SECRETS_DIR})",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init_cmd = commands.add_parser("init", help="create the missing secret files")
    init_cmd.add_argument("--principal", type=principal_argument, default="claude-code")
    token_cmd = commands.add_parser("token", help="replace a principal's token")
    token_cmd.add_argument("principal", type=principal_argument)
    page_cmd = commands.add_parser(
        "setup-page", help="open the setup page while no passkey is registered"
    )
    page_cmd.add_argument("--port", type=int, default=8767)
    claude_cmd = commands.add_parser(
        "claude-code-json", help="print Claude Code's entry for the gateway"
    )
    claude_cmd.add_argument(
        "--principal", type=principal_argument, default="claude-code"
    )
    claude_cmd.add_argument("--port", type=int, default=8765)
    claude_cmd.add_argument(
        "--helper", type=Path, required=True, help="the path of bin/maji-headers"
    )
    desktop_cmd = commands.add_parser(
        "connect-claude-desktop",
        help="add the gateway to Claude Desktop's configuration",
    )
    desktop_cmd.add_argument("--config", type=Path, required=True)
    desktop_cmd.add_argument(
        "--principal", type=principal_argument, default="claude-code"
    )
    desktop_cmd.add_argument("--port", type=int, default=8765)

    # The relay is installed beside the Python running this, in the project's
    # environment. The path to that Python is kept as it is, unresolved: resolved, it
    # leads to the interpreter the environment was made from.
    #
    desktop_cmd.add_argument(
        "--relay", type=Path, default=Path(sys.executable).parent / "maji-relay"
    )
    desktop_cmd.add_argument(
        "--replace",
        action="store_true",
        help="replace a different server already named maji",
    )
    args = parser.parse_args()

    if args.command == "connect-claude-desktop":
        try:
            entry = desktop_entry(
                args.secrets, args.principal, args.port, args.relay.absolute()
            )
            added = connect_desktop(args.config, entry, replace_existing=args.replace)
        except (OSError, ValueError) as exc:
            sys.exit(f"setup: {exc}")
        if added:
            print(
                f"setup: added maji to {args.config}; open Claude Desktop to start it"
            )
        else:
            print(f"setup: {args.config} already has maji, as this would add it")
        return

    if args.command == "claude-code-json":
        try:
            entry = claude_entry(args.secrets, args.principal, args.port, args.helper)
        except FileNotFoundError as exc:
            sys.exit(f"setup: {exc}")
        print(json.dumps(entry))
        return
    if args.command == "init":
        for line in init(args.secrets, args.principal):
            print(f"setup: {line}")
        return
    if args.command == "setup-page":
        print(f"setup: {setup_page(args.port)}")
        return
    rotate(args.secrets, args.principal)
    print(
        f"setup: new token for {args.principal} in {args.secrets}/"
        f"{args.principal}.token; restart the gateway (make restart "
        "SERVICE=gateway), then connect your app again (make reconnect-claude-code "
        "does all three for Claude Code)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
