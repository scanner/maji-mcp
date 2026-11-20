"""Tests for how the runner looks through a path for files named the way secrets are."""

# system imports
import time
from pathlib import Path

# 3rd party imports
import pytest

# project imports
from maji_mcp.runner.secrets import secrets_in


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """
    A repository holding files named like secrets, files that look alike but aren't,
    dependencies, a symlink, a remote URL with a token in its config, and a clone
    inside it whose remote URL has none.
    """
    repo = tmp_path / "api"
    files = {
        ".env": "TOKEN=x",
        ".env.local": "TOKEN=x",
        ".env.example": "TOKEN=",
        "deploy/server.pem": "",
        "config/client_secret.json": "{}",
        "src/secret_test.go": "",
        "src/app.py": "",
        ".kube/config": "",
        "infra/prod.tfstate": "{}",
        "node_modules/pkg/.env": "TOKEN=x",
        "README.md": "",
        ".git/config": '[remote "origin"]\n\turl = https://me:ghp_abc@github.com/me/api\n',
        "vendor/lib/.git/config": "\turl = git@github.com:me/lib.git\n",
    }
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    (tmp_path / "outside.env").write_text("TOKEN=x")
    (repo / "linked.env").symlink_to(tmp_path / "outside.env")
    return repo


def test_files_named_like_secrets_are_found(repo: Path, deadline: float) -> None:
    """
    GIVEN: A repository holding `.env` files, a key, a client secret, a kubeconfig,
           Terraform state, and a remote URL with a token in its git config
     WHEN: the runner looks through it
     THEN: it finds each of them by name, and the git config, and says the look was
           complete
      AND: it leaves out an example `.env`, code whose name says "secret", files in
           dependencies, a symlink, which an executor would resolve inside itself,
           and the config of a clone whose remote URL holds no password
    """
    found = secrets_in(repo, deadline)
    assert sorted(found.files) == [
        ".env",
        ".env.local",
        ".git/config",
        ".kube/config",
        "config/client_secret.json",
        "deploy/server.pem",
        "infra/prod.tfstate",
    ]
    assert not found.itself
    assert found.complete


@pytest.mark.parametrize(
    ("asked", "itself"),
    [("deploy/server.pem", True), (".git/config", True), ("README.md", False)],
    ids=["a key", "a git config holding a token", "a README"],
)
def test_a_file_asked_for_is_judged_by_its_own_name(
    repo: Path, deadline: float, asked: str, itself: bool
) -> None:
    """
    GIVEN: A key file, a git config holding a remote URL with a token, and a README
     WHEN: each is asked for on its own
     THEN: the key and the config are each said to be a file named like a secret
           itself, which a mount of it couldn't leave out, and the README isn't
    """
    found = secrets_in(repo / asked, deadline)
    assert found.itself is itself
    assert found.files == ([Path(asked).name] if itself else [])


def test_a_folder_to_skip_is_left_unopened(repo: Path, deadline: float) -> None:
    """
    GIVEN: A repository, and a folder in it that a mount covers whole
     WHEN: the runner looks through the repository, told to skip that folder
     THEN: the files in the folder aren't named, since nothing in it will be read
    """
    found = secrets_in(repo, deadline, skip=[repo / "deploy"])
    assert "deploy/server.pem" not in found.files
    assert ".env" in found.files


def test_a_git_folder_asked_for_is_checked_for_its_config(
    repo: Path, deadline: float
) -> None:
    """
    GIVEN: A repository's `.git` folder, whose config holds a remote URL with a token
     WHEN: the folder is looked through on its own
     THEN: its config is named
    """
    assert secrets_in(repo / ".git", deadline).files == ["config"]


def test_a_look_stops_at_its_limits_and_says_so(repo: Path, deadline: float) -> None:
    """
    GIVEN: A repository
     WHEN: the runner looks through it with room for only a few entries, or past its
           deadline
     THEN: it stops, and says the look wasn't complete and how far it got
    """
    few = secrets_in(repo, deadline, max_entries=3)
    assert not few.complete
    assert few.looked_at == 3
    late = secrets_in(repo, time.monotonic() - 1)
    assert not late.complete
