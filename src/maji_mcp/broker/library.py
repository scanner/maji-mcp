"""
The skill library: the skills agents saved, as files on a volume only the broker
mounts.

One directory per skill, and one per version of it:

    <root>/<publisher>/<pack>/<name>/
        <version>/skill.py        the code
        <version>/manifest.json   what it is: its function, signature, summary, and
                                  what it calls and imports
        <version>/SKILL.md        the same, written for people and for packs
        <version>/saves.json      each time it was kept: principal, session, run,
                                  when, and what that run called
        <version>/review.json     the item asking you to promote it, if the agent
                                  asked, and what became of it
        promoted.json             the version you promoted, and your decision
        revoked.json              the versions you demoted

A version is named by the hash of its code and its manifest together, so changing
either, the summary every agent reads included, makes a new version
(`docs/design/planned/sharing.md` decision 2). The same code kept twice, by two
sessions say, is one version kept twice.

Every skill is under `<publisher>/local` for now. Packs are planned, and the
publisher comes from the instance's configuration (decision 9).

What may run is decided here by state, never by where a file is (decision 10):
- **a kept version** runs for the principal that kept it, in any of its sessions:
  the last one it kept, unless you have promoted a version since. A skill runs with
  its caller's permissions, so keeping one gives the agent nothing a script doesn't,
  and needs no decision. Its summary is shown to that principal alone, labelled as
  not reviewed.
- **a promoted version** you haven't demoted, whose files still hash to what you
  promoted, runs for any principal allowed `skills.<name>`. Promotion is asked for
  to share a skill with other agents, or to trust a skill that writes.
- **a demoted version** runs for no one, the principals that kept it included: you
  demote a version to stop that code. An agent can keep a changed version, which runs
  for it as any kept version does.

Promotion is the decision you made in the queue, which the control plane keeps with
its signature. Checking that signature here, for libraries shared between machines,
is planned. Until then the volume is the boundary: only the broker mounts it.

Each file is written whole and moved into place, so a reader never sees half of one.
"""

# system imports
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# project imports
from maji_mcp.broker.skill_code import check_name
from maji_mcp.common.protocol import MajiError
from maji_mcp.control.canonical import canonical, sha256

PACK = "local"
VERSION_CHARS = 16

# What became of a version's promotion item.
#
WAITING = "waiting"
PROMOTED = "promoted"
CHANGES_REQUESTED = "changes_requested"
REJECTED = "rejected"
SUPERSEDED = "superseded"
CONFLICT = "conflict"
NOT_ASKED = "not_asked"
DEMOTED = "demoted"


def now() -> str:
    """
    The time, to the microsecond: the latest save of a session is found by comparing
    these, and one session can save twice in a second.
    """
    return datetime.now(UTC).isoformat(timespec="microseconds")


def digest_of(source: str, manifest: dict[str, Any]) -> str:
    """
    The hash a version is named by: of its code and its manifest, in the canonical
    JSON a decision is hashed in, so a promotion signature can cover it as it is.
    """
    return sha256(canonical({"manifest": manifest, "source": source}))


###############################################################################
#
@dataclass(frozen=True)
class Version:
    """
    One version of a skill, as saved.

    Args:
        name: the skill's name
        version: the first `VERSION_CHARS` of its digest, its directory's name
        digest: the SHA-256 of its code and manifest, as `sha256:<hex>`
        source: its code
        manifest: what it is
    """

    name: str
    version: str
    digest: str
    source: str
    manifest: dict[str, Any]

    @property
    def summary(self) -> str:
        return str(self.manifest.get("summary", ""))


@dataclass
class Review:
    """
    A version's promotion item and what became of it.

    Args:
        item: the queue item, or None when there was no queue to ask
        state: one of the states above
        digest: the version's digest when the item was raised, which a promotion
            has to match
        replaces: the version promoted when the item was raised, which has to be
            the one promoted still when you decide, or None
        note: your note, when you asked for changes or rejected it
        decision: the decision on the item
        at: when the state last changed
    """

    item: str | None
    state: str
    digest: str
    replaces: str | None = None
    note: str | None = None
    decision: str | None = None
    at: str = field(default_factory=now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "item": self.item,
            "state": self.state,
            "digest": self.digest,
            "replaces": self.replaces,
            "note": self.note,
            "decision": self.decision,
            "at": self.at,
        }

    @classmethod
    def of(cls, data: dict[str, Any]) -> Review:
        return cls(
            item=data.get("item"),
            state=str(data["state"]),
            digest=str(data["digest"]),
            replaces=data.get("replaces"),
            note=data.get("note"),
            decision=data.get("decision"),
            at=str(data.get("at") or now()),
        )


###############################################################################
#
class Library:
    """
    The skills on one instance.

    Args:
        root: the library's directory
        publisher: the namespace this instance's skills are saved under
    """

    def __init__(self, root: Path, publisher: str) -> None:
        check_name(publisher.replace("-", "_"))
        self.root = root
        self.publisher = publisher

    def skill_id(self, name: str) -> str:
        """A skill's full name: `<publisher>/<pack>/<name>`."""
        return f"{self.publisher}/{PACK}/{check_name(name)}"

    def _dir(self, name: str) -> Path:
        return self.root / self.publisher / PACK / check_name(name)

    def _version_dir(self, name: str, version: str) -> Path:
        if len(version) != VERSION_CHARS or not all(
            c in "0123456789abcdef" for c in version
        ):
            raise MajiError("invalid_argument", f"{version!r} isn't a version")
        return self._dir(name) / version

    ###########################################################################
    # Saving and reading versions

    def save(
        self, name: str, source: str, manifest: dict[str, Any], saved: dict[str, Any]
    ) -> tuple[Version, bool]:
        """
        Save a version, or record another save of one already here.

        Args:
            name: the skill's name
            source: its code
            manifest: what it is
            saved: who saved it, from which session and run, and when

        Returns:
            The version, and whether it is new
        """
        digest = digest_of(source, manifest)
        version = digest.removeprefix("sha256:")[:VERSION_CHARS]
        final = self._version_dir(name, version)
        created = False
        if not final.exists():
            final.parent.mkdir(parents=True, exist_ok=True)
            building = Path(tempfile.mkdtemp(prefix=f".{version}-", dir=final.parent))
            try:
                (building / "skill.py").write_text(source)
                _write_json(building / "manifest.json", manifest)
                (building / "SKILL.md").write_text(skill_md(manifest))
                _write_json(building / "saves.json", [])
                try:
                    building.rename(final)
                    created = True
                except OSError:
                    if not final.exists():
                        raise
            finally:
                if building.exists():
                    shutil.rmtree(building)
        saves = _read_json(final / "saves.json", [])
        saves.append(saved)
        _write_json(final / "saves.json", saves)
        found = self.version(name, version)
        assert found is not None
        return found, created

    def version(self, name: str, version: str) -> Version | None:
        """
        A version as it is on disk, or None if it's missing or its files no longer
        hash to its name.
        """
        path = self._version_dir(name, version)
        try:
            source = (path / "skill.py").read_text()
            manifest = json.loads((path / "manifest.json").read_text())
        except FileNotFoundError, json.JSONDecodeError:
            return None
        digest = digest_of(source, manifest)
        if digest.removeprefix("sha256:")[:VERSION_CHARS] != version:
            return None
        return Version(name, version, digest, source, manifest)

    def names(self) -> list[str]:
        """Every skill with at least one version."""
        base = self.root / self.publisher / PACK
        if not base.is_dir():
            return []
        return sorted(
            p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")
        )

    def versions(self, name: str) -> Iterator[str]:
        """A skill's versions, by name, in no order."""
        base = self._dir(name)
        if base.is_dir():
            for path in base.iterdir():
                if path.is_dir() and not path.name.startswith("."):
                    yield path.name

    def saves(self, name: str, version: str) -> list[dict[str, Any]]:
        """Each save of a version, oldest first."""
        found: list[dict[str, Any]] = _read_json(
            self._version_dir(name, version) / "saves.json", []
        )
        return found

    ###########################################################################
    # Reviews and promotion

    def review(self, name: str, version: str) -> Review | None:
        data = _read_json(self._version_dir(name, version) / "review.json", None)
        return Review.of(data) if data else None

    def set_review(self, name: str, version: str, review: Review) -> None:
        _write_json(self._version_dir(name, version) / "review.json", review.as_dict())

    def promoted_record(self, name: str) -> dict[str, Any] | None:
        found: dict[str, Any] | None = _read_json(
            self._dir(name) / "promoted.json", None
        )
        return found

    def promoted(self, name: str) -> Version | None:
        """
        The version of a skill you promoted, if it is intact and you haven't
        demoted it.
        """
        record = self.promoted_record(name)
        if not record:
            return None
        found = self.version(name, str(record["version"]))
        if found is None or found.digest != record.get("digest"):
            return None
        if any(r.get("digest") == found.digest for r in self.revoked(name)):
            return None
        return found

    def promote(self, name: str, version: Version, record: dict[str, Any]) -> None:
        """
        Make a version the one every principal gets, from now: a version a principal
        kept before this is set aside for this one, and one kept after it isn't.
        """
        _write_json(
            self._dir(name) / "promoted.json",
            {
                "version": version.version,
                "digest": version.digest,
                **record,
                "promoted_at": now(),
            },
        )

    def revoked(self, name: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = _read_json(self._dir(name) / "revoked.json", [])
        return found

    def revocation(self, name: str, version: str) -> dict[str, Any] | None:
        """Your demotion of a version, if you demoted it."""
        return next(
            (r for r in reversed(self.revoked(name)) if r.get("version") == version),
            None,
        )

    def demote(self, name: str, record: dict[str, Any]) -> Version | None:
        """
        Take a promoted skill back: record its version as revoked, so it never loads
        again, and leave no version promoted.

        Returns:
            The version demoted, or None if none was promoted
        """
        current = self.promoted(name)
        if current is None:
            return None
        revoked = self.revoked(name)
        revoked.append({"version": current.version, "digest": current.digest, **record})
        _write_json(self._dir(name) / "revoked.json", revoked)
        (self._dir(name) / "promoted.json").unlink(missing_ok=True)
        return current

    def waiting(self) -> dict[str, tuple[str, str]]:
        """The promotion items still waiting, each with its skill and version."""
        found = {}
        for name in self.names():
            for version in self.versions(name):
                review = self.review(name, version)
                if review and review.item and review.state == WAITING:
                    found[review.item] = (name, version)
        return found

    def by_item(self, item: str) -> tuple[str, str] | None:
        """The skill and version a promotion item asked about."""
        for name in self.names():
            for version in self.versions(name):
                review = self.review(name, version)
                if review and review.item == item:
                    return name, version
        return None

    ###########################################################################
    # What may run

    def kept(self, name: str, principal: str) -> Version | None:
        """The version of a skill a principal kept last, in any of its sessions."""
        latest: tuple[str, str] | None = None
        for version in self.versions(name):
            for save in self.saves(name, version):
                if save.get("principal") == principal and (
                    latest is None or str(save.get("at")) > latest[0]
                ):
                    latest = (str(save.get("at")), version)
        return self.version(name, latest[1]) if latest else None

    def kept_at(self, name: str, version: str, principal: str) -> str | None:
        """When a principal last kept a version."""
        times = [
            str(s.get("at"))
            for s in self.saves(name, version)
            if s.get("principal") == principal
        ]
        return max(times) if times else None

    def loadable(self, name: str, principal: str) -> tuple[Version, str]:
        """
        The version of a skill a principal runs: the one it kept last, if it kept it
        after you last promoted one, so it can try a new version of a promoted skill;
        or else the promoted one.

        Returns:
            The version, and `kept` or `promoted`

        Raises:
            MajiError: `not_found` when there is neither, with the skills the
                principal may run
        """
        found = self._loadable(name, principal)
        if found is not None:
            return found
        available = self.visible_names(principal)
        demotion = self.demotion_for(name, principal)
        if demotion is not None:
            raise MajiError(
                "not_found",
                demoted_text(name, demotion)
                + " No agent runs that version now; keep a changed version with "
                "save_skill to run one again",
                {"available": available},
            )
        raise MajiError(
            "not_found",
            f"no skill {name!r} you may run: a skill runs for the agent that kept "
            "it, or for every agent once the user has promoted it",
            {"available": available},
        )

    def demotion_for(self, name: str, principal: str) -> dict[str, Any] | None:
        """
        Your demotion that leaves a principal no version of a skill to run: of the
        version it kept last, or else your latest of the skill.
        """
        if not self._dir(name).is_dir():
            return None
        kept = self.kept(name, principal)
        if kept is not None:
            found = self.revocation(name, kept.version)
            if found is not None:
                return found
        revoked = self.revoked(name)
        return revoked[-1] if revoked else None

    def _loadable(self, name: str, principal: str) -> tuple[Version, str] | None:
        kept, promoted = self.kept(name, principal), self.promoted(name)
        if kept is not None and self.revocation(name, kept.version) is not None:
            kept = None
        if (
            kept is not None
            and promoted is not None
            and kept.version != promoted.version
        ):
            record = self.promoted_record(name) or {}
            at = self.kept_at(name, kept.version, principal) or ""
            if at < str(record.get("promoted_at") or ""):
                kept = None
        if kept is not None and (promoted is None or kept.version != promoted.version):
            return kept, "kept"
        return (promoted, "promoted") if promoted is not None else None

    def visible(self, principal: str) -> list[tuple[Version, str]]:
        """Each skill a principal may run, with which kind of version it gets."""
        return [
            found
            for name in self.names()
            if (found := self._loadable(name, principal)) is not None
        ]

    def visible_names(self, principal: str) -> list[str]:
        return [f"skills.{v.name}" for v, _ in self.visible(principal)]


###############################################################################
#
def demoted_text(name: str, demotion: dict[str, Any]) -> str:
    """
    Say which version of a skill you demoted, when, and your note on it, as a
    sentence ending once, however your note ends.
    """
    text = (
        f"the user demoted skills.{name}, version {demotion.get('version')}, on "
        f"{str(demotion.get('at', ''))[:10]}"
    )
    if demotion.get("note"):
        text += f": {demotion['note']}"
    return text if text.endswith((".", "!", "?")) else text + "."


def skill_md(manifest: dict[str, Any]) -> str:
    """A skill's `SKILL.md`: its summary, signature and docstring, for people."""
    lines = [
        f"# {manifest['name']}",
        "",
        str(manifest.get("summary", "")),
        "",
        "```python",
        str(manifest.get("signature", "")),
        "```",
    ]
    if manifest.get("docstring"):
        lines += ["", str(manifest["docstring"])]
    calls = manifest.get("calls") or []
    if calls:
        lines += ["", "Calls: " + ", ".join(calls)]
    return "\n".join(lines) + "\n"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def _write_json(path: Path, value: Any) -> None:
    """Write a file whole, then move it into place."""
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, indent=2, sort_keys=True)
            out.write("\n")
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
