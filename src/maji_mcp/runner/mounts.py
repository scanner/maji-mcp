"""
Which host paths a run may have mounted, resolved the way the Docker daemon will see
them.

An agent names the paths a script needs. The runner resolves each one here before the
broker sees it: the item you decide shows the resolved path, and the grant covers that
path. The runner resolves it again just before creating the container, and binds it
only if it is still the path granted (`sandbox.validate`).

The daemon resolves a bind source itself, so a path through a symlink binds wherever
the symlink leads. Resolving here, against your home directory mounted read-only at
the same path, sees the same target the daemon will. Symlinks *inside* a mounted folder
are resolved in the executor's own filesystem, so they can't lead back out to yours.

Some places are never mounted, whatever is granted. These are built in, and removing
one is a change to maji-mcp, never a setting:
- a root itself, such as your whole home directory
- anything in a hidden folder at the top of a root, such as `~/.ssh`, `~/.config` or
  `~/.rd` and `~/.docker` (where Rancher Desktop and Docker Desktop keep their Docker
  sockets), and `~/Library`
- each path in `deny`, which is maji-mcp's own secrets folder, and any folder that
  holds one

Your own never-mount list adds to them, and can only narrow what is mounted: a file of
paths, one a line, read again before each run, so an edit counts from the next run.
A path on it, and anything inside one, is refused like the ones above. A list that
can't be read as one refuses every mount until it is fixed, rather than letting
through what it was meant to keep out.

A folder that holds a listed path is mounted, with the listed path covered: a script
reads it as empty (`runner/sandbox.py`, `Cover`). The files named the way secrets are
kept are covered the same way (`runner/secrets.py`). So a project is one grant,
whatever it holds, and what it holds that a script isn't to read stays unread.
`covers_in` works the covers out, before a path is put to you and before every run.
"""

# system imports
import os
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

# project imports
from maji_mcp.common.protocol import MajiError
from maji_mcp.runner.sandbox import MAX_COVERS, MOUNT_NAME, Cover, Mount
from maji_mcp.runner.secrets import secrets_in

MAX_MOUNTS = 10
NEVER_AT_TOP = frozenset({"library"})  # compared case-folded
MAX_LIST_BYTES = 64 * 1024


###############################################################################
#
@dataclass(frozen=True)
class MountRules:
    """
    Where mounts may come from.

    Args:
        roots: the folders a mount must be inside, such as your home directory
        deny: paths never mounted, nor any folder that holds them
        never: the paths on your never-mount list, as last read
        never_file: where your never-mount list is kept, if you keep one
        home: your home directory, which `~` stands for in the list
        problem: why the list couldn't be read, which refuses every mount
    """

    roots: tuple[Path, ...]
    deny: tuple[Path, ...] = ()
    never: tuple[Path, ...] = ()
    never_file: Path | None = None
    home: Path | None = None
    problem: str | None = None

    @classmethod
    def of(
        cls,
        roots: Sequence[str | Path],
        deny: Sequence[str | Path] = (),
        never_file: str | Path | None = None,
        home: str | Path | None = None,
    ) -> MountRules:
        """
        Build the rules from paths as configured, resolving each one.

        The rules compare resolved paths, so a root reached through a symlink, such
        as `/tmp` on macOS, still contains what resolves beneath it.

        Raises:
            ValueError: a path in `deny` doesn't exist. A path never mounted is
                configured from where the stack was started, such as maji-mcp's own
                secrets; one that names nothing is a mistake that would leave the
                real one mountable, so the runner doesn't start.
        """
        missing = [str(d) for d in deny if not os.path.exists(d)]
        if missing:
            raise ValueError(f"paths never to mount don't exist: {missing}")
        return cls(
            roots=tuple(_true_case(Path(os.path.realpath(r))) for r in roots),
            deny=tuple(_true_case(Path(os.path.realpath(d))) for d in deny),
            never_file=Path(never_file) if never_file else None,
            home=Path(home) if home else None,
        )

    def current(self) -> MountRules:
        """
        These rules with your never-mount list as it reads now.

        A run takes them once and checks its paths against them both times it
        resolves them, so an edit to the list in between can't make the two disagree.
        """
        if self.never_file is None:
            return self
        try:
            never = read_never_mount(self.never_file, self.home)
        except ValueError as exc:
            return replace(self, never=(), problem=str(exc))
        return replace(self, never=never, problem=None)


def read_never_mount(path: Path, home: Path | None) -> tuple[Path, ...]:
    """
    Read your never-mount list: one path a line, absolute or starting `~/`, with
    blank lines and lines starting `#` left out. A path needn't exist yet.

    Returns:
        Each path resolved as the runner resolves a path asked for, so one reached
        through a symlink still matches; none when there is no list

    Raises:
        ValueError: the list can't be read, or a line isn't a path it can use
    """
    try:
        with open(path, "rb") as listed:
            raw = listed.read(MAX_LIST_BYTES + 1)
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise ValueError(f"{path} can't be read: {exc.strerror}") from None
    if len(raw) > MAX_LIST_BYTES:
        raise ValueError(f"{path} is longer than {MAX_LIST_BYTES} bytes")
    try:
        text = raw.decode()
    except UnicodeDecodeError:
        raise ValueError(f"{path} isn't text") from None

    paths: list[Path] = []
    for number, line in enumerate(text.splitlines(), 1):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry == "~" or entry.startswith("~/"):
            if home is None:
                raise ValueError(f"{path}, line {number}: ~ needs the runner's --home")
            entry = str(home) + entry[1:]
        if "\0" in entry or not os.path.isabs(entry):
            raise ValueError(
                f"{path}, line {number}: {entry!r} isn't an absolute path or one "
                "starting ~/"
            )
        paths.append(_true_case(Path(os.path.realpath(entry))))
    return tuple(paths)


###############################################################################
#
def resolve(requested: object, rules: MountRules) -> Path:
    """
    Resolve a path an agent asked to mount, and check it may be mounted.

    Args:
        requested: an absolute path to a folder or a file
        rules: the roots and the paths never mounted

    Returns:
        The path with every symlink resolved, spelled as the filesystem stores
        it, which is what the grant covers and what gets bound

    Raises:
        MajiError: `invalid_argument` when it isn't an absolute path to an existing
            folder or file, and `permission_denied` when it is outside the roots or
            in a place never mounted
    """
    # The agent reads these refusals, and your never-mount list is your own record
    # of places to keep from it, so they never name what is on it: not the entry a
    # path holds, nor a line the list can't use. The runner's log says which line.
    #
    if rules.problem is not None:
        raise MajiError(
            "permission_denied",
            "nothing is mounted until the user fixes their never-mount list, which "
            "has a line the runner can't use",
        )
    if not isinstance(requested, str) or not requested or "\0" in requested:
        raise MajiError("invalid_argument", "a mount is an absolute path")
    if not os.path.isabs(requested):
        raise MajiError("invalid_argument", f"{requested} isn't an absolute path")
    try:
        real = _true_case(Path(os.path.realpath(requested, strict=True)))
    except OSError:
        raise MajiError("invalid_argument", f"{requested} doesn't exist") from None
    if not (real.is_dir() or real.is_file()):
        raise MajiError("invalid_argument", f"{requested} isn't a folder or a file")

    # A SQLite database in WAL mode keeps its newest rows in a file beside it, which
    # a mount of the database alone would leave out, without any error on reading.
    #
    if real.is_file() and os.path.exists(f"{real}-wal"):
        raise MajiError(
            "invalid_argument",
            f"mount the folder holding {real.name}: its newest rows are in "
            f"{real.name}-wal beside it",
        )

    root = next((r for r in rules.roots if real.is_relative_to(r)), None)
    if root is None:
        raise MajiError(
            "permission_denied",
            f"{real} is outside the folders that may be mounted",
            {"roots": [str(r) for r in rules.roots]},
        )
    if real == root:
        raise MajiError(
            "permission_denied",
            f"{real} is never mounted whole, nor searched: ask the user where what "
            "you're looking for is, and ask for that folder",
        )
    top = real.relative_to(root).parts[0]
    if top.startswith(".") or top.casefold() in NEVER_AT_TOP:
        raise MajiError("permission_denied", f"{root / top} is never mounted")
    for denied in rules.deny:
        if _within(real, denied) or _within(denied, real):
            raise MajiError("permission_denied", f"{real} holds or is in {denied}")
    for listed in rules.never:
        if _within(real, listed):
            raise MajiError(
                "permission_denied",
                f"{real} is, or is in, a place the user never has mounted: ask them "
                "where else to look",
            )
    return real


###############################################################################
#
@dataclass(frozen=True)
class LeftOut:
    """
    What a mount of one path leaves out, and whether the path can be mounted that way.

    Args:
        secrets: the files named like secrets, relative to the path, each covered
        listed: the paths on your never-mount list inside the path, relative to it,
            each covered, with whether it is a folder
        itself: whether the path is a file named like a secret. Nothing covers it: it
            was asked for by name.
        looked_at: how many entries were looked at
    """

    secrets: tuple[str, ...] = ()
    listed: tuple[tuple[str, bool], ...] = ()
    itself: bool = False
    looked_at: int = 0

    @property
    def count(self) -> int:
        return len(self.secrets) + len(self.listed)

    def covers(self, mount: str) -> list[Cover]:
        """The covers for a mount of the path, under the mount's name."""
        return [Cover(mount, path, folder) for path, folder in self.listed] + [
            Cover(mount, path) for path in self.secrets
        ]


def covers_in(
    path: Path,
    rules: MountRules,
    deadline: float,
    asked: Collection[Path] = (),
) -> LeftOut:
    """
    Work out what a mount of `path` leaves out: the files in it named like secrets,
    and the places in it on your never-mount list.

    Args:
        path: a resolved path that may be mounted (`resolve`)
        rules: the rules it was resolved under, with your never-mount list
        deadline: the `time.monotonic()` by which the look through it must end
        asked: the paths this run asks for by name. A file among them is mounted on
            its own grant, so the mount of the folder holding it leaves it readable.

    Returns:
        What is left out. A path that is a file leaves nothing out.

    Raises:
        MajiError: `invalid_argument` when the folder can't be looked through within
            the bounds, or holds more to cover than a run can. Either way some of
            what a script isn't to read could go uncovered, so nothing is mounted.
    """
    if not path.is_dir():
        found = secrets_in(path, deadline)
        return LeftOut(itself=found.itself, looked_at=found.looked_at)

    # A listed path is covered where it is a real folder or file inside this one. One
    # that doesn't exist yet has nothing to cover, and a link is left as it is: inside
    # an executor it leads wherever its target is there, and a cover over it would
    # land on that target.
    #
    # The shallowest listed paths come first, so one inside a listed folder is left to
    # that folder's cover.
    #
    depth = len(path.parts)
    inside_it = sorted(
        (
            path.joinpath(*entry.parts[depth:])
            for entry in rules.never
            if _within(entry, path) and len(entry.parts) > depth
        ),
        key=lambda found: len(found.parts),
    )
    listed: list[tuple[str, bool]] = []
    whole: list[Path] = []
    for inside in inside_it:
        if inside.is_symlink() or not inside.exists():
            continue
        if any(_within(inside, folder) for folder in whole):
            continue
        listed.append((inside.relative_to(path).as_posix(), inside.is_dir()))
        if inside.is_dir():
            whole.append(inside)

    found = secrets_in(path, deadline, skip=whole)
    if not found.complete:
        raise MajiError(
            "invalid_argument",
            f"{path} holds more than can be looked through for files named like "
            f"secrets before a run (it stopped after {found.looked_at} entries): ask "
            "for a folder inside it",
        )
    named = {str(p) for p in asked}
    taken = {rel for rel, _ in listed}
    secrets = [
        rel for rel in found.files if str(path / rel) not in named and rel not in taken
    ]
    if len(secrets) + len(listed) > MAX_COVERS:
        raise MajiError(
            "invalid_argument",
            f"{path} holds {len(secrets) + len(listed)} files a mount would leave "
            f"out, more than the {MAX_COVERS} a run can: ask for a folder inside it",
        )
    return LeftOut(
        secrets=tuple(secrets), listed=tuple(listed), looked_at=found.looked_at
    )


def _true_case(path: Path) -> Path:
    """
    Return `path` spelled as the filesystem stores it.

    On a case-insensitive disk, such as macOS's by default, `realpath` keeps the case
    it was given, and `~/library` opens `~/Library`. Each part is matched against its
    folder's listing, so the checks, the item and the grant all see the stored name.
    """
    current = Path(path.anchor)
    for part in path.parts[1:]:
        try:
            entries = os.listdir(current)
        except OSError:
            entries = []
        if part not in entries:
            alike = [e for e in entries if e.casefold() == part.casefold()]
            if len(alike) == 1:
                part = alike[0]
        current = current / part
    return current


def _within(path: Path, folder: Path) -> bool:
    """
    Whether `path` is `folder` or inside it, ignoring case.

    On a case-sensitive disk this refuses a few more paths than it must, the safe
    side for a check on places never mounted.
    """
    return Path(str(path).casefold()).is_relative_to(Path(str(folder).casefold()))


def resolve_all(requested: object, rules: MountRules) -> list[Path]:
    """
    Resolve a run's list of paths, each once, in the order asked.

    Raises:
        MajiError: `invalid_argument` for a list that isn't one, or is longer than
            `MAX_MOUNTS`, and whatever `resolve` raises for a path in it
    """
    if not isinstance(requested, list) or len(requested) > MAX_MOUNTS:
        raise MajiError(
            "invalid_argument", f"mounts is a list of at most {MAX_MOUNTS} paths"
        )
    resolved: list[Path] = []
    for path in requested:
        real = resolve(path, rules)
        if real not in resolved:
            resolved.append(real)
    return resolved


def named(paths: Sequence[Path]) -> list[Mount]:
    """
    Give each path the name a script sees it under: `/mnt/<name>`.

    The name is the path's last part, with any character outside letters, digits,
    `.`, `_` and `-` made a `-`. When two end alike, the later ones get `-2`, `-3`
    and so on, in the order asked, so the same list always gets the same names.
    """
    mounts: list[Mount] = []
    taken: set[str] = set()
    for path in paths:
        base = re.sub(r"[^A-Za-z0-9._-]", "-", path.name).lstrip("._-")[:60] or "mount"
        name, n = base, 1
        while name in taken:
            n += 1
            name = f"{base}-{n}"
        assert MOUNT_NAME.fullmatch(name), name
        taken.add(name)
        mounts.append(Mount(source=str(path), name=name))
    return mounts
