"""
Where secrets are likely to be in a path an agent asks to mount.

A mount of a folder leaves out the files named the way secrets usually are: `.env`
files, keys and certificates, `credentials` files, `.netrc`, kubeconfigs, Terraform
state, and a repository's `.git/config` when a remote URL in it carries a password or
token. The runner covers each one inside the executor, so a script reads it as empty
(`runner/sandbox.py`, `Cover`). The look here says which files those are, before a path
is put to you and again before every run, since files come and go.

A file named like a secret is read only when an agent asks for that file by name. That
request is an item of its own, high risk, with the reason **secrets in reach**
(`docs/design/control-plane.md`, "What makes an item high risk").

The look goes by names, so it is a guess: a token pasted into an ordinary config file
isn't found. It never reads a file's contents but `.git/config`'s, and what it reports
is only ever names.

The look is bounded, since the runner's view of your files crosses Docker's file
share: it stops after `MAX_ENTRIES` entries or at a deadline, and says it didn't
finish, which refuses the mount (`runner/mounts.py`). It doesn't descend into
dependency and cache folders, such as `node_modules`, or into `.git`, and never follows
a symlink: inside an executor, a symlink resolves in the executor's own filesystem, and
one to a file in the mount is found under its own name. It runs no git: the runner
holds the Docker socket, and a repository's config can name commands for git to run.
"""

# system imports
import os
import re
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

MAX_ENTRIES = 50_000
MAX_CONFIG_BYTES = 64 * 1024

# Matched against a file's name, case-folded.
#
NAMES = frozenset(
    {
        ".env",
        ".netrc",
        "_netrc",
        ".npmrc",
        ".pypirc",
        ".pgpass",
        ".htpasswd",
        ".git-credentials",
        ".dockercfg",
        "kubeconfig",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
SUFFIXES = (
    ".env",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".ppk",
    ".kubeconfig",
    ".tfstate",
    ".tfstate.backup",
    ".tfvars",
)

# `.env.local` holds values, while `.env.example` and its kind show which values a
# project needs, without them.
#
EXAMPLES = frozenset({"example", "sample", "template", "dist", "defaults"})

# A file whose name says "secret" or "credential" is one when it holds data rather
# than code: `client_secret.json`, `credentials`, but not `secret_test.go`.
#
WORDS = ("secret", "credential")
DATA_SUFFIXES = frozenset(
    {"", ".json", ".yaml", ".yml", ".toml", ".ini", ".txt", ".conf", ".cfg", ".xml"}
)

# A file named `config` holds secrets in these folders.
#
CONFIG_FOLDERS = frozenset({".kube", ".docker", ".aws"})

SKIPPED = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

# A remote URL with a password or token in it, such as https://me:ghp_x@github.com.
#
URL_WITH_PASSWORD = re.compile(rb"://[^/\s:@]+:[^/\s@]+@")


###############################################################################
#
@dataclass
class Found:
    """
    What a look through one path found.

    Args:
        files: every file named like a secret, relative to the path. For a path that
            is itself such a file, its own name.
        itself: whether the path is a file named like a secret, which a mount of it
            can't cover
        complete: whether the look went through the whole path
        looked_at: how many entries it looked at
    """

    files: list[str] = field(default_factory=list)
    itself: bool = False
    complete: bool = True
    looked_at: int = 0


def secrets_in(
    path: Path,
    deadline: float,
    max_entries: int = MAX_ENTRIES,
    skip: Collection[Path] = (),
) -> Found:
    """
    Look through a folder or file for files named the way secrets usually are.

    Args:
        path: a resolved path the runner checked may be mounted
        deadline: the `time.monotonic()` by which to stop looking
        max_entries: how many entries to look at before stopping
        skip: folders inside `path` to leave unopened, such as ones a mount covers
            whole

    Returns:
        What was found, and whether the look went through the whole path
    """
    found = Found()
    if path.is_file():
        found.looked_at = 1
        if likely_secret(path.name, path.parent.name) or (
            path.name == "config"
            and path.parent.name == ".git"
            and _holds_credential(path)
        ):
            found.files.append(path.name)
            found.itself = True
        return found

    # A repository's config is read where its `.git` folder is met in a listing, which
    # costs nothing more for the folders without one. `.git` is never opened further,
    # so a path that is itself a `.git` folder is the one case checked apart.
    #
    if path.name == ".git" and _holds_credential(path / "config"):
        found.files.append("config")
    unopened = {str(folder) for folder in skip}
    folders = [path]
    while folders:
        folder = folders.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in sorted(entries, key=lambda e: e.name):
            if found.looked_at >= max_entries or time.monotonic() > deadline:
                found.complete = False
                return found
            found.looked_at += 1
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                config = Path(entry.path) / "config"
                if entry.name == ".git" and _holds_credential(config):
                    found.files.append(os.path.relpath(config, path))
                if entry.name not in SKIPPED and entry.path not in unopened:
                    folders.append(Path(entry.path))
            elif likely_secret(entry.name, folder.name):
                found.files.append(os.path.relpath(entry.path, path))
    return found


def likely_secret(name: str, folder: str) -> bool:
    """Whether a file's name, in a folder of this name, is one secrets are kept under."""
    lower = name.casefold()
    if lower in NAMES:
        return True
    if lower.startswith(".env."):
        return lower.rsplit(".", 1)[-1] not in EXAMPLES
    if lower.endswith(SUFFIXES):
        return True
    if lower in ("config", "config.json") and folder.casefold() in CONFIG_FOLDERS:
        return True
    stem, suffix = os.path.splitext(lower)
    return suffix in DATA_SUFFIXES and any(word in stem for word in WORDS)


def _holds_credential(config: Path) -> bool:
    """
    Whether a repository's config has a remote URL with a password or token in it.

    A config that is a symlink is left unread: a cover goes over a file, and inside an
    executor the link leads wherever its target is there.
    """
    if config.is_symlink():
        return False
    try:
        with open(config, "rb") as read:
            text = read.read(MAX_CONFIG_BYTES)
    except OSError:
        return False
    return URL_WITH_PASSWORD.search(text) is not None
