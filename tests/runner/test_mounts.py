"""Tests for how the runner resolves and checks the paths a run asks to mount."""

# system imports
import time
from collections.abc import Callable
from pathlib import Path

# 3rd party imports
import pytest

# project imports
from maji_mcp.common.protocol import MajiError
from maji_mcp.runner import mounts
from maji_mcp.runner.mounts import (
    MAX_MOUNTS,
    MountRules,
    covers_in,
    named,
    resolve,
    resolve_all,
)
from maji_mcp.runner.sandbox import Cover
from maji_mcp.runner.server import rules_from


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """
    A home directory with a repository, a profile, a hidden folder, `Library`, and
    maji-mcp's own checkout with its secrets, beside a folder outside it. Three links
    in it lead to the repository, to the folder outside, and to the hidden folder.
    """
    home = tmp_path / "home"
    for folder in (
        "src/api/.git",
        "src/maji-mcp/secrets",
        "profiles",
        ".ssh",
        "Library/Keychains",
    ):
        (home / folder).mkdir(parents=True)
    (home / "profiles" / "cpu.json").write_text("{}")
    (tmp_path / "elsewhere").mkdir()
    (home / "api-link").symlink_to(home / "src" / "api")
    (home / "src" / "out").symlink_to(tmp_path / "elsewhere")
    (home / "src" / "keys").symlink_to(home / ".ssh")
    return home


@pytest.fixture
def rules(home: Path) -> MountRules:
    """Rules that mount from `home`, and never from maji-mcp's secrets folder in it."""
    return MountRules.of([home], deny=[home / "src" / "maji-mcp" / "secrets"])


@pytest.fixture
def never_mount(home: Path, tmp_path: Path) -> Callable[..., MountRules]:
    """A function that returns rules whose never-mount list holds the lines given."""

    def listing(*lines: str) -> MountRules:
        listed = tmp_path / "never-mount"
        listed.write_text("\n".join(lines) + "\n")
        return MountRules.of([home], never_file=listed, home=home).current()

    return listing


@pytest.mark.parametrize(
    ("asked", "real"),
    [
        ("src/api", "src/api"),
        ("profiles/cpu.json", "profiles/cpu.json"),
        ("api-link", "src/api"),
    ],
    ids=["a folder", "a file", "a link to a folder"],
)
def test_a_path_resolves_to_where_it_really_is(
    home: Path, rules: MountRules, asked: str, real: str
) -> None:
    """
    GIVEN: A repository and a profile inside the home directory, and a link to the
           repository
     WHEN: resolve checks each
     THEN: each is returned as its resolved absolute path, the link as the
           repository's own, so the grant and the item name what will be bound
    """
    assert resolve(str(home / asked), rules) == (home / real).resolve()


@pytest.mark.parametrize(
    ("asked", "message"),
    [
        ("../elsewhere", "outside the folders"),
        ("", "ask the user where"),
        (".ssh", "never mounted"),
        ("Library/Keychains", "never mounted"),
        ("src/maji-mcp/secrets", "holds or is in"),
        ("src/maji-mcp", "holds or is in"),
        ("src", "holds or is in"),
        ("src/out", "outside the folders"),
        ("src/keys", "never mounted"),
    ],
)
def test_places_never_mounted_are_refused(
    home: Path, rules: MountRules, asked: str, message: str
) -> None:
    """
    GIVEN: A path outside the home directory, the home directory itself, a hidden
           folder at its top, `Library`, maji-mcp's secrets, a folder holding them,
           or a link inside the home directory that leads to one of those
     WHEN: resolve checks it
     THEN: it is refused as `permission_denied`, whatever a grant might say, a link
           judged by where it leads and not where it sits
    """
    with pytest.raises(MajiError, match=message) as raised:
        resolve(str(home / asked), rules)
    assert raised.value.code == "permission_denied"


def test_a_path_in_another_case_is_judged_by_its_stored_name(
    home: Path, rules: MountRules
) -> None:
    """
    GIVEN: A case-insensitive disk, such as macOS's by default, and paths typed in
           another case: a repository, `~/library`, and maji-mcp's secrets in capitals
     WHEN: resolve checks them
     THEN: the repository comes back spelled as stored, so its grant names it once
      AND: `~/library` and the secrets are refused as the places they are
    """
    if not (home / "SRC").exists():
        pytest.skip("this disk is case-sensitive")
    assert resolve(str(home / "SRC" / "API"), rules) == (home / "src" / "api").resolve()
    with pytest.raises(MajiError, match="never mounted"):
        resolve(str(home / "library" / "Keychains"), rules)
    with pytest.raises(MajiError, match="holds or is in"):
        resolve(str(home / "src" / "MAJI-MCP" / "SECRETS"), rules)
    with pytest.raises(MajiError, match="holds or is in"):
        resolve(str(home / "Src" / "Maji-Mcp"), rules)


@pytest.mark.parametrize(
    ("requested", "message"),
    [
        ("src/api", "absolute path"),
        ("", "absolute path"),
        (None, "absolute path"),
        ("/no/such/folder", "doesn't exist"),
    ],
)
def test_a_path_that_names_nothing_is_an_invalid_argument(
    rules: MountRules, requested: object, message: str
) -> None:
    """
    GIVEN: A relative path, an empty one, a non-string, or one that doesn't exist
     WHEN: resolve checks it
     THEN: it is refused as `invalid_argument`
    """
    with pytest.raises(MajiError, match=message) as raised:
        resolve(requested, rules)
    assert raised.value.code == "invalid_argument"


def test_a_database_with_its_newest_rows_beside_it_is_mounted_by_its_folder(
    home: Path, rules: MountRules
) -> None:
    """
    GIVEN: A SQLite database in WAL mode with its `-wal` file beside it, as a log
           capture leaves it
     WHEN: resolve checks the database alone, and then its folder
     THEN: the database alone is refused, naming the folder to mount instead, since
           its newest rows would be left out without any error
      AND: the folder is accepted
    """
    captures = home / "captures"
    captures.mkdir()
    (captures / "worker.db").write_bytes(b"")
    (captures / "worker.db-wal").write_bytes(b"")
    with pytest.raises(MajiError, match="mount the folder holding worker.db"):
        resolve(str(captures / "worker.db"), rules)
    assert resolve(str(captures), rules) == captures.resolve()


def test_a_list_resolves_each_path_once_in_order(home: Path, rules: MountRules) -> None:
    """
    GIVEN: A run's list naming one repository twice, once through a symlink
     WHEN: resolve_all checks the list
     THEN: each resolved path appears once, in the order first asked
      AND: a list longer than MAX_MOUNTS, or one that isn't a list, is refused
    """
    paths = [str(home / "profiles"), str(home / "src" / "api"), str(home / "api-link")]
    assert resolve_all(paths, rules) == [
        (home / "profiles").resolve(),
        (home / "src" / "api").resolve(),
    ]
    with pytest.raises(MajiError, match="at most"):
        resolve_all([str(home / "profiles")] * (MAX_MOUNTS + 1), rules)
    with pytest.raises(MajiError, match="list"):
        resolve_all(str(home / "profiles"), rules)


def test_names_are_last_parts_made_unique() -> None:
    """
    GIVEN: Paths that end alike, and one whose last part has spaces and a leading dot
     WHEN: named gives each its place under /mnt
     THEN: each name is its path's last part, the later of two alike get -2, and
           characters outside letters, digits, `.`, `_` and `-` become `-`
    """
    mounts = named(
        [
            Path("/h/work/api"),
            Path("/h/old/api"),
            Path("/h/My Profiles"),
            Path("/h/.hidden name"),
        ]
    )
    assert [m.name for m in mounts] == ["api", "api-2", "My-Profiles", "hidden-name"]
    assert mounts[1].source == "/h/old/api"
    assert mounts[1].target == "/mnt/api-2"


def test_a_path_on_your_list_is_refused_and_a_folder_holding_it_is_not(
    home: Path, never_mount: Callable[..., MountRules]
) -> None:
    """
    GIVEN: Your never-mount list naming a folder inside a repository, with a comment
           and a blank line
     WHEN: resolve checks that folder, a folder inside it, the repository holding it,
           and a folder elsewhere
     THEN: the listed folder and the folder inside it are refused as
           `permission_denied`, telling the agent to ask you where else to look,
           without naming what is on your list
      AND: the repository resolves, since a mount of it covers the listed folder
      AND: the folder elsewhere resolves as before
    """
    (home / "src" / "api" / "rules" / "drafts").mkdir(parents=True)
    rules = never_mount("# client work", "", "~/src/api/rules")

    for asked in (home / "src/api/rules", home / "src/api/rules/drafts"):
        with pytest.raises(MajiError, match="ask them where else to look") as refused:
            resolve(str(asked), rules)
        assert refused.value.code == "permission_denied"
    assert resolve(str(home / "src" / "api"), rules) == (home / "src/api").resolve()
    assert resolve(str(home / "profiles"), rules) == (home / "profiles").resolve()


@pytest.fixture
def project(home: Path) -> Path:
    """
    A repository holding code, files named like secrets, a folder and a file you keep
    private, and a link.
    """
    project = (home / "src" / "api").resolve()
    files = {
        "app.py": "print('hi')",
        ".env": "TOKEN=x",
        "deploy/server.pem": "",
        "private/notes.txt": "",
        "private/.env": "TOKEN=x",
        "private/deeper/more.txt": "",
        "plans.md": "",
    }
    for name, text in files.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(text)
    (project / "link-to-private").symlink_to("private")
    return project


def test_a_mount_leaves_out_secrets_and_what_is_on_your_list(
    project: Path, never_mount: Callable[..., MountRules], deadline: float
) -> None:
    """
    GIVEN: A repository holding a `.env` and a key, with a folder and a file in it on
           your never-mount list, a second listed folder inside the first, a listed
           path that doesn't exist, and a listed path that is a link
     WHEN: covers_in works out what a mount of the repository leaves out
     THEN: it leaves out the two files named like secrets, the listed folder as a
           folder and the listed file as a file
      AND: nothing inside the listed folder is left out again, since the folder is
           covered whole, nor the path that doesn't exist, nor the link, which an
           executor resolves inside itself
      AND: each becomes a cover at its path inside the mount
    """
    rules = never_mount(
        "~/src/api/private",
        "~/src/api/private/deeper",
        "~/src/api/plans.md",
        "~/src/api/not-yet",
        "~/src/api/link-to-private",
    )

    out = covers_in(project, rules, deadline)

    assert sorted(out.secrets) == [".env", "deploy/server.pem"]
    assert sorted(out.listed) == [("plans.md", False), ("private", True)]
    assert not out.itself and out.count == 4
    assert sorted(out.covers("api"), key=lambda c: c.path) == [
        Cover("api", ".env"),
        Cover("api", "deploy/server.pem"),
        Cover("api", "plans.md"),
        Cover("api", "private", folder=True),
    ]


def test_a_file_asked_for_by_name_isnt_left_out_of_its_folder(
    home: Path, project: Path, deadline: float
) -> None:
    """
    GIVEN: A run that asks for a repository and, by name, for the `.env` in it
     WHEN: covers_in works out what the mount of the repository leaves out
     THEN: that `.env` isn't left out, since the run mounts it on a grant of its own,
           and the key and another `.env` deeper in still are
      AND: for the `.env` asked for on its own, it says the path is itself a file
           named like a secret, which nothing covers
    """
    rules = MountRules.of([home])
    asked = [project, project / ".env"]

    left_out = covers_in(project, rules, deadline, asked).secrets
    assert sorted(left_out) == ["deploy/server.pem", "private/.env"]
    alone = covers_in(project / ".env", rules, deadline, asked)
    assert alone.itself and alone.count == 0
    assert not covers_in(project / "app.py", rules, deadline, asked).itself


def test_a_folder_that_cant_be_looked_through_isnt_mounted(
    home: Path, project: Path, deadline: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    GIVEN: A repository the look can't finish in time, and one holding more files to
           cover than a run may have
     WHEN: covers_in works out what a mount leaves out
     THEN: each is refused as `invalid_argument`, telling the agent to ask for a
           folder inside it: some of what a script isn't to read could go uncovered
    """
    rules = MountRules.of([home])
    with pytest.raises(MajiError, match="ask for a folder inside it") as late:
        covers_in(project, rules, time.monotonic() - 1)
    assert late.value.code == "invalid_argument"

    # The real limit is far more files than a readable fixture holds.
    #
    monkeypatch.setattr(mounts, "MAX_COVERS", 1)
    with pytest.raises(MajiError, match="more than the 1 a run can") as many:
        covers_in(project, rules, deadline)
    assert many.value.code == "invalid_argument"


def test_the_list_is_read_again_for_each_run(home: Path, tmp_path: Path) -> None:
    """
    GIVEN: Rules whose never-mount list is a file you edit
     WHEN: the list doesn't exist yet, then names a folder, then names it through a
           symlink to it
     THEN: with no list nothing more is refused; once the folder is listed, under its
           own name or through the symlink, it is refused from the next run on
    """
    listed = tmp_path / "never-mount"
    rules = MountRules.of([home], never_file=listed, home=home)
    profiles = str(home / "profiles")
    assert resolve(profiles, rules.current()) == Path(profiles).resolve()

    listed.write_text(f"{profiles}\n")
    with pytest.raises(MajiError):
        resolve(profiles, rules.current())

    (home / "profiles-link").symlink_to(home / "profiles")
    listed.write_text("~/profiles-link\n")
    with pytest.raises(MajiError):
        resolve(profiles, rules.current())


@pytest.mark.parametrize(
    "line",
    ["src/api", "~someone/src", "~/src\0api"],
    ids=["a relative path", "another user's home", "a NUL"],
)
def test_a_list_that_cant_be_read_as_one_refuses_every_mount(
    home: Path, never_mount: Callable[..., MountRules], line: str
) -> None:
    """
    GIVEN: A never-mount list with a line that isn't a path it can use
     WHEN: resolve checks a folder the list doesn't name
     THEN: it is refused as `permission_denied`, so a mistake in the list keeps
           everything out rather than what it was meant to
      AND: the refusal, which the agent reads, names nothing on the list; the rules
           keep which line it was, for the runner's log
    """
    rules = never_mount("~/profiles", line)
    with pytest.raises(MajiError, match="fixes their never-mount list") as refused:
        resolve(str(home / "src" / "api"), rules)
    assert refused.value.code == "permission_denied"
    assert "profiles" not in refused.value.message
    assert rules.problem is not None and "line 2" in rules.problem


def test_mounts_need_maji_mcps_secrets_folder(home: Path) -> None:
    """
    GIVEN: The runner's arguments
     WHEN: its mount rules are built from them
     THEN: with no folder to mount from, it mounts nothing
      AND: with folders to mount from, it needs maji-mcp's secrets folder, and one
           that exists, or it doesn't start: started from somewhere its secrets
           folder isn't, the real secrets would be left mountable
    """
    secrets = str(home / "src" / "maji-mcp" / "secrets")
    assert rules_from([], None, None, None) is None
    with pytest.raises(ValueError, match="--secrets-dir is required"):
        rules_from([str(home)], None, None, None)
    with pytest.raises(ValueError, match="don't exist"):
        rules_from([str(home)], str(home / "nowhere"), None, None)
    rules = rules_from([str(home)], secrets, None, None)
    assert rules is not None and rules.deny == (Path(secrets).resolve(),)
