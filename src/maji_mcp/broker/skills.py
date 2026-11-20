"""
Skills in the broker: keeping one, asking you to promote it, serving it to a run,
describing it to an agent, and applying what you decide.

**Keeping.** The gateway sends the script an agent tried its function in, and the run
it tried it in, if any. The broker checks that run against its own run log: the same
principal, the same script, and it exited 0. It takes the function out of the script
(`skill_code.extract`) and keeps it as a new version (`library.py`), with what that
run called. Keeping needs no decision: a skill runs with its caller's permissions, so
the agent gets nothing a script wouldn't give it. The agent that kept a skill calls
it from any of its sessions, and refines it by keeping it again.

**Promoting.** An agent asks for promotion only when it changes something: to share
a skill with other agents, or to trust one that writes. The item it raises shows:
- the code, as a new file, or as a diff from the version promoted now
- the summary every agent will read once it is promoted
- what the skill reaches: the stubs it reads and writes with, the skills it calls,
  and the modules it imports, as read from its code
- anything it reaches by a name worked out at run time, flagged
- what the run it came from called, flagged where the code doesn't name it

How risky promotion is follows from that reach, not from the skill being code
(`docs/design/control-plane.md`, "What makes an item high risk"). A skill that writes,
or whose reach can't all be read from its code, is high risk, with the reason
`trust_changes`: once promoted, any agent may run it. One that only reads is an ordinary
item, which takes no passkey of its own and may be decided in a batch. Asking again
replaces the item still waiting, so the queue holds one item per skill and principal.

Whether a stub reads or writes isn't worked out from code either: each is classified
when its service is set up (`Stub.writes`), and the broker checks every call as it
is made, so what a run reached is in the run log whatever its code looked like.

**Serving.** A script calls `skills.<name>(...)`. The stub client asks for the skill
over the call socket with its run's token (`load_skill`), and the broker answers with
its code if the run's principal may call `skills.<name>`, is within its rate limit,
and the library has a version it may load: the one it kept, or the promoted one. The
code then runs in the script's own process, with the script's own permissions: every
stub it calls is checked as the script's would be.

**Deciding.** Your decision reaches the broker through the control plane's decision
feed (`decisions.py`): promote makes the version the one every agent gets, if its
files and the version promoted are still as they were when the item was raised.
Request changes and reject record your note, which the agent sees when it reads the
skill, keeps it again, or asks again. A note reaches an agent as one line of printable
text, whichever decision it came with.
"""

# system imports
import difflib
import hashlib
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# project imports
from maji_mcp.broker import skill_code
from maji_mcp.broker.catalog import Catalog
from maji_mcp.broker.library import (
    CHANGES_REQUESTED,
    CONFLICT,
    DEMOTED,
    NOT_ASKED,
    PROMOTED,
    REJECTED,
    SUPERSEDED,
    WAITING,
    Library,
    Review,
    Version,
    demoted_text,
    now,
)
from maji_mcp.broker.policy import Policy
from maji_mcp.broker.runlog import RunLog
from maji_mcp.broker.runs import Run
from maji_mcp.common.names import session_name
from maji_mcp.common.protocol import MajiError, call_unix

MAX_SCRIPT_BYTES = 512 * 1024
MAX_SUMMARY = 200
MAX_WHY = 2000
MAX_NOTE = 2000

# What each state of a promotion means to the agent reading it.
#
STATES = {
    WAITING: "waiting for the user",
    PROMOTED: "promoted",
    CHANGES_REQUESTED: "the user asked for changes",
    REJECTED: "the user declined to promote it",
    SUPERSEDED: "replaced by a later request",
    CONFLICT: "not promoted: another version was promoted, or its files changed, "
    "after it was put to the user",
    NOT_ASKED: "not put to the user: there was no queue to ask",
    DEMOTED: "demoted by the user: no agent runs it",
}


###############################################################################
#
@dataclass(frozen=True)
class Reach:
    """
    What a skill reaches when it runs, through the skills it calls too.

    Args:
        reads: the stubs it reads with
        writes: the stubs it writes with
        skills: the skills it calls
        unknown: the stubs it names that the broker doesn't serve
        unseen: what can't be read from its code: names worked out at run time, and
            stubs its run called that the code doesn't name
    """

    reads: tuple[str, ...]
    writes: tuple[str, ...]
    skills: tuple[str, ...]
    unknown: tuple[str, ...]
    unseen: tuple[str, ...]


class SkillService:
    """
    The broker's part in skills.

    Args:
        library: where skills are kept
        catalog: the stubs, whose namespaces are a skill's globals, and which say
            whether each reads or writes
        policy: who may call which skill, as `skills.<name>`
        runlog: what runs did, for the evidence a kept skill cites
        control_plane: the control plane's socket for the broker, for promotion
            items; without one, skills are kept but can't be promoted
    """

    def __init__(
        self,
        library: Library,
        catalog: Catalog,
        policy: Policy,
        runlog: RunLog,
        control_plane: Path | None = None,
    ) -> None:
        self.library = library
        self.catalog = catalog
        self.policy = policy
        self.runlog = runlog
        self.control_plane = control_plane

    def namespaces(self) -> frozenset[str]:
        """The stub namespaces a skill's code may reach, as globals."""
        return frozenset({s.namespace for s in self.catalog.stubs.values()} - {"maji"})

    def allows(self, principal: str, name: str) -> bool:
        return self.policy.allows(principal, f"skills.{name}")

    def _checked(self, params: dict[str, Any], verb: str) -> tuple[str, str]:
        """The principal and the skill's name, if the principal may call it."""
        principal = _text(params, "principal", 200)
        name = skill_code.check_name(params.get("name"))
        if not self.allows(principal, name):
            raise MajiError(
                "permission_denied", f"{principal} may not {verb} skills.{name}"
            )
        return principal, name

    ###########################################################################
    # Keeping

    def keep(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Keep a skill from the script it was tried in, for the principal that kept it.

        Params:
            principal, session: who keeps it, as the gateway authenticated them
            name: the skill's name, and its function's
            summary: one line saying what it does and when to use it, which the
                agent reads, and every agent once it is promoted
            script: the script, or just the function's code
            run: the run that script succeeded in, or None
            client: what the agent's app said it is

        Returns:
            What was kept, what it reaches, any promotion of an earlier version still
            waiting, and your note on the last one you decided

        Raises:
            MajiError: `permission_denied` when the principal may not call the
                skill, `invalid_argument` for a run that isn't this script's
                success, or code that can't be a skill
        """
        principal, name = self._checked(params, "keep")
        session = params.get("session")
        summary = _one_line(_text(params, "summary", MAX_SUMMARY))
        if not summary:
            raise MajiError("invalid_argument", "summary is required")
        script = _text(params, "script", MAX_SCRIPT_BYTES)
        run = _optional_text(params, "run", 64)
        ran = self._run_evidence(principal, run, script)

        namespaces = self.namespaces()
        extracted = skill_code.extract(script, name, namespaces)
        found = skill_code.analyse(extracted.source, namespaces)
        whole = skill_code.analyse(script, namespaces)
        manifest = {
            "id": self.library.skill_id(name),
            "name": name,
            "runtime": "python",
            "entry": extracted.entry,
            "summary": summary,
            "signature": extracted.signature(),
            "docstring": extracted.docstring,
            "params": [p.as_dict() for p in extracted.params],
            "returns": extracted.returns,
            "input": extracted.input_schema,
            "output": extracted.output_schema,
            "calls": list(found.calls),
            "imports": list(found.imports),
            "requires": {
                "stubs": {
                    ns: self.catalog.versions[ns]
                    for ns in found.stub_namespaces
                    if ns in self.catalog.versions
                },
                "skills": sorted(
                    self.library.skill_id(c.split(".", 1)[1])
                    for c in found.calls
                    if c.startswith("skills.") and skill_code.NAME.match(c[7:])
                ),
            },
            "derived_from": None,
            "lockfile": None,
        }
        saved: dict[str, Any] = {
            "principal": principal,
            "session": session,
            "at": now(),
        }
        if ran is not None:
            # What the run called that the script doesn't name was reached by a name
            # worked out at run time, which may have been the skill's doing.
            #
            saved["run"] = run
            saved["ran"] = ran
            saved["unnamed"] = sorted(
                stub
                for stub in ran["calls"]
                if stub not in whole.calls and not stub.startswith(("skills.", "maji."))
            )
        version, _ = self.library.save(name, extracted.source, manifest, saved)
        reach = self.reach(version, principal)
        promoted = self.library.promoted(name)
        waiting = self._waiting_for(name, principal, other_than=version.version)
        demotion = self.library.revocation(name, version.version)
        if demotion is not None:
            state = DEMOTED
        elif promoted is not None and promoted.version == version.version:
            state = PROMOTED
        else:
            state = "kept"
        return {
            "id": manifest["id"],
            "name": name,
            "version": version.version,
            "signature": manifest["signature"],
            "reads": list(reach.reads),
            "writes": list(reach.writes),
            "dynamic": list(found.dynamic),
            "dropped": [list(span) for span in extracted.dropped],
            "state": state,
            "demoted_on": str(demotion.get("at", ""))[:10] if demotion else None,
            "waiting": waiting,
            "note": (
                demotion.get("note")
                if demotion is not None
                else self._last_note(name, principal, version.version)
            ),
        }

    def _run_evidence(
        self, principal: str, run: str | None, script: str
    ) -> dict[str, Any] | None:
        """
        What the run a kept skill cites did, checked against the run log: this
        principal's, of this script, and it exited 0 in time.

        Raises:
            MajiError: `invalid_argument` for a run that isn't that
        """
        if run is None:
            return None
        record = self.runlog.run(run)
        if (
            record is None
            or record["principal"] != principal
            or record["script_sha256"] != hashlib.sha256(script.encode()).hexdigest()
        ):
            raise MajiError(
                "invalid_argument", f"run {run[:8]} isn't a run of this script"
            )
        if record["exit_code"] != 0 or record["timed_out"]:
            raise MajiError(
                "invalid_argument",
                f"run {run[:8]} didn't succeed; keep a function from a run that exited 0",
            )
        return {
            "duration_ms": record["duration_ms"],
            "calls": dict(Counter(s for s, _ in self.runlog.calls(run))),
        }

    def _waiting_for(self, name: str, principal: str, other_than: str) -> str | None:
        """The item still asking you to promote another version this principal kept."""
        for version in self.library.versions(name):
            review = self.library.review(name, version)
            if (
                version != other_than
                and review is not None
                and review.state == WAITING
                and self.library.kept_at(name, version, principal) is not None
            ):
                return review.item
        return None

    def _last_note(self, name: str, principal: str, version: str) -> str | None:
        """Your note on the last of this principal's other versions you decided."""
        latest: tuple[str, str] | None = None
        for other in self.library.versions(name):
            review = self.library.review(name, other)
            if other == version or review is None or not review.note:
                continue
            if self.library.kept_at(name, other, principal) is not None and (
                latest is None or review.at > latest[0]
            ):
                latest = (review.at, review.note)
        return latest[1] if latest else None

    ###########################################################################
    # What a skill reaches

    def reach(
        self, version: Version, principal: str, seen: frozenset[str] = frozenset()
    ) -> Reach:
        """
        What a version reaches: the stubs its code names and those its runs called
        without naming, and through each skill it calls, the version this principal
        would get of that skill.
        """
        namespaces = self.namespaces()
        found = skill_code.analyse(version.source, namespaces)
        stubs = {c for c in found.calls if not c.startswith(("maji.", "skills."))}
        unseen = list(found.dynamic)
        for save in self.library.saves(version.name, version.version):
            for stub in save.get("unnamed") or []:
                stubs.add(stub)
                unseen.append(f"its run called {stub}, which the code doesn't name")
        skills = sorted(c[7:] for c in found.calls if c.startswith("skills."))
        reads: set[str] = set()
        writes: set[str] = set()
        unknown: set[str] = set()
        for stub in stubs:
            served = self.catalog.stubs.get(stub)
            if served is None:
                unknown.add(stub)
            else:
                (writes if served.writes else reads).add(stub)
        for name in skills:
            if name in seen or name == version.name:
                continue
            try:
                called, _ = self.library.loadable(name, principal)
            except MajiError:
                unseen.append(f"it calls skills.{name}, which isn't one it may run")
                continue
            inner = self.reach(called, principal, seen | {version.name})
            reads |= set(inner.reads)
            writes |= set(inner.writes)
            unknown |= set(inner.unknown)
            unseen += [f"through skills.{name}: {u}" for u in inner.unseen]
        return Reach(
            reads=tuple(sorted(reads)),
            writes=tuple(sorted(writes)),
            skills=tuple(f"skills.{s}" for s in skills),
            unknown=tuple(sorted(unknown)),
            unseen=tuple(dict.fromkeys(unseen)),
        )

    ###########################################################################
    # Promoting

    async def promote(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Ask you to promote the version of a skill the principal kept last.

        Params:
            principal, session: who asks, as the gateway authenticated them
            name: the skill
            why: the agent's reason, shown as its claim
            client: what the agent's app said it is

        Returns:
            The version, its state, the item that asks you, and how risky it is

        Raises:
            MajiError: `not_found` when the principal has kept no such skill,
                `unavailable` when there is no queue to ask
        """
        principal, name = self._checked(params, "promote")
        if self.control_plane is None:
            raise MajiError("unavailable", "there is no queue to ask the user")
        session = params.get("session")
        why = _optional_text(params, "why", MAX_WHY)
        version = self.library.kept(name, principal)
        if version is None:
            raise MajiError(
                "not_found", f"you have kept no skills.{name}; keep it with save_skill"
            )
        demotion = self.library.revocation(name, version.version)
        if demotion is not None:
            raise MajiError(
                "invalid_argument",
                demoted_text(name, demotion)
                + " Keep a changed version with save_skill, and ask to promote that",
            )
        current = self.library.promoted(name)
        review = self.library.review(name, version.version)
        reach = self.reach(version, principal)
        answer: dict[str, Any] = {
            "name": name,
            "version": version.version,
            "writes": list(reach.writes),
            "session_name": session_name(session) if session else None,
            "note": self._last_note(name, principal, version.version),
        }
        if current is not None and current.version == version.version:
            return {**answer, "state": PROMOTED, "item": None, "risk": None}
        if review is not None and review.state == WAITING:
            return {**answer, "state": WAITING, "item": review.item, "risk": None}

        await self._supersede(name, principal, version.version)
        saves = [
            s
            for s in self.library.saves(name, version.version)
            if s.get("principal") == principal
        ]
        last = saves[-1] if saves else {}
        requester = {
            "principal": principal,
            **({"session": session} if session else {}),
            **({"run": last["run"]} if last.get("run") else {}),
            **({"client": params["client"]} if params.get("client") else {}),
        }
        facts, risk = _facts(version, current, reach, last)
        item = await call_unix(
            self.control_plane,
            "create_item",
            {
                "kind": "promote",
                "title": (
                    f"Promote a new version of skills.{name}"
                    if current
                    else f"Promote skills.{name}"
                ),
                "subject": f"skills.{name} · {version.manifest['id']}",
                "skill": {"id": version.manifest["id"], "version": version.version},
                "requester": requester,
                "facts": facts,
                **risk,
                "diff": {
                    "target": f"skills/{version.manifest['id']}/skill.py",
                    **({} if current else {"new": True}),
                    "lines": _diff(current.source if current else "", version.source),
                },
                "claim": why,
            },
        )
        self.library.set_review(
            name,
            version.version,
            Review(
                item=str(item["id"]),
                state=WAITING,
                digest=version.digest,
                replaces=current.version if current else None,
            ),
        )
        return {**answer, "state": WAITING, "item": item["id"], "risk": item["risk"]}

    async def _supersede(self, name: str, principal: str, version: str) -> None:
        """
        Withdraw the items still waiting for this principal's other versions of a
        skill, so a new request replaces them in the queue.
        """
        for other in self.library.versions(name):
            review = self.library.review(name, other)
            if other == version or review is None or review.state != WAITING:
                continue
            if self.library.kept_at(name, other, principal) is None:
                continue
            if review.item and self.control_plane is not None:
                await call_unix(
                    self.control_plane,
                    "withdraw",
                    {"item": review.item, "principal": principal},
                )
            review.state, review.at = SUPERSEDED, now()
            self.library.set_review(name, other, review)

    ###########################################################################
    # Serving a run

    def load(self, run: Run, name: object) -> dict[str, Any]:
        """
        A skill's code, for a run to call it.

        Raises:
            MajiError: `permission_denied` when the run's principal may not call it,
                `not_found` when there is no version it may load
        """
        checked = skill_code.check_name(name)
        if not self.allows(run.principal, checked):
            raise MajiError(
                "permission_denied", f"{run.principal} may not call skills.{checked}"
            )
        version, kind = self.library.loadable(checked, run.principal)
        return {
            "id": version.manifest["id"],
            "name": checked,
            "version": version.version,
            "kind": kind,
            "entry": version.manifest["entry"],
            "source": version.source,
            "filename": f"<skill {version.manifest['id']}@{version.version}>",
        }

    ###########################################################################
    # Describing

    def _visible(self, principal: str) -> list[tuple[Version, str]]:
        return [
            (v, kind)
            for v, kind in self.library.visible(principal)
            if self.allows(principal, v.name)
        ]

    def listed(self, principal: str) -> list[dict[str, str]]:
        """The skills a principal may call, each with its summary, for `list_api`."""
        return [
            {
                "name": f"skills.{v.name}",
                "summary": v.summary
                + ("" if kind == "promoted" else " [kept by you, not reviewed]"),
            }
            for v, kind in self._visible(principal)
        ]

    def search(self, principal: str, query: str) -> list[dict[str, str]]:
        """
        The skills a principal may call whose name, summary or docstring mention
        every word of the query.
        """
        words = query.lower().split()
        found = []
        for v, _ in self._visible(principal):
            text = " ".join(
                [v.name, v.summary, str(v.manifest.get("docstring", ""))]
            ).lower()
            if all(w in text for w in words):
                found.append(
                    {
                        "name": f"skills.{v.name}",
                        "signature": str(v.manifest["signature"]),
                        "summary": v.summary,
                    }
                )
        return found

    def read(self, principal: str, name: str) -> str | None:
        """
        A skill's documentation and code, for `read_api`: `skills` for every skill
        the principal may call, or `skills.<name>` for one.

        Returns:
            The text, or None when there's no such skill for this caller
        """
        if name == "skills":
            docs = [
                self._doc(v, kind, principal) for v, kind in self._visible(principal)
            ]
            return "\n\n".join(docs) if docs else None
        wanted = name.removeprefix("skills.")
        if not name.startswith("skills.") or not skill_code.NAME.match(wanted):
            return None
        if not self.allows(principal, wanted):
            return None
        try:
            version, kind = self.library.loadable(wanted, principal)
        except MajiError:
            demotion = self.library.demotion_for(wanted, principal)
            if demotion is None:
                return None
            return (
                f"skills.{wanted}: {demoted_text(wanted, demotion)} No agent runs "
                "that version now; keep a changed version with save_skill to run one "
                "again."
            )
        return self._doc(version, kind, principal)

    def _doc(self, version: Version, kind: str, principal: str) -> str:
        manifest = version.manifest
        lines = [str(manifest["signature"]), "", version.summary]
        if manifest.get("docstring"):
            lines += ["", str(manifest["docstring"])]
        reach = self.reach(version, principal)
        lines += [
            "",
            "Reads with: " + (", ".join(reach.reads) if reach.reads else "no stubs"),
        ]
        if reach.writes:
            lines.append("Writes with: " + ", ".join(reach.writes))
        if reach.skills:
            lines.append("Calls: " + ", ".join(reach.skills))
        label = f"{manifest['id']}, version {version.version}"
        review = self.library.review(version.name, version.version)
        if kind == "promoted":
            record = self.library.promoted_record(version.name) or {}
            lines.append(
                f"Promoted: {label}, on {str(record.get('promoted_at', ''))[:10]}"
            )
        else:
            lines.append(
                f"Kept by you, not reviewed: {label}. Other agents can call it once "
                "the user promotes it."
            )
            if review is not None:
                lines.append(
                    f"Promotion: {STATES.get(review.state, review.state)}"
                    + (f", item {review.item}" if review.item else "")
                )
            revoked = self.library.revoked(version.name)
            if revoked:
                lines.append(f"Earlier, {demoted_text(version.name, revoked[-1])}")
        note = review.note if review is not None and review.note else None
        note = note or self._last_note(version.name, principal, version.version)
        if note:
            lines.append(f"The user's note: {note}")
        lines += ["", "Source:", version.source.rstrip("\n")]
        return "\n".join(lines)

    ###########################################################################
    # Applying decisions

    async def apply(
        self, decision: dict[str, Any], item: dict[str, Any]
    ) -> tuple[str, str] | None:
        """
        Apply your decision on a promotion item, or your demotion on top of it.

        Returns:
            The outcome to record, `applied` or `conflict`, and what it did; or None
            for an item that isn't a promotion this library raised, or one already
            applied
        """
        if item.get("kind") != "promote":
            return None
        found = self.library.by_item(str(item.get("id")))
        if found is None:
            return None
        name, version_id = found
        review = self.library.review(name, version_id)
        if review is None:
            return None
        action = decision.get("action")
        if action == "demote":
            return self._demote(name, version_id, review, decision, item)
        if review.state != WAITING:
            return None
        review.decision, review.at = str(decision.get("id")), now()
        if action == "promote":
            version = self.library.version(name, version_id)
            current = self.library.promoted(name)
            if version is None or version.digest != review.digest:
                review.state = CONFLICT
                self.library.set_review(name, version_id, review)
                return (
                    "conflict",
                    f"skills.{name}'s files changed after it was put to you",
                )
            # A version demoted since this one was put to you leaves nothing
            # promoted, and this one still replaces it: the diff you read was from it.
            #
            replaced = current.version if current else None
            if replaced != review.replaces and not (
                current is None
                and review.replaces is not None
                and self.library.revocation(name, review.replaces) is not None
            ):
                review.state = CONFLICT
                self.library.set_review(name, version_id, review)
                return (
                    "conflict",
                    f"another version of skills.{name} was promoted after this one was "
                    "put to you",
                )
            self.library.promote(
                name,
                version,
                {
                    "item": item["id"],
                    "decision": decision.get("id"),
                    "decision_hash": decision.get("decision_hash"),
                    "by": decision.get("by"),
                    "at": decision.get("at"),
                },
            )
            review.state = PROMOTED
            self.library.set_review(name, version_id, review)
            return "applied", f"promoted skills.{name}, version {version_id}"
        if action in ("request_changes", "reject"):
            review.state = (
                CHANGES_REQUESTED if action == "request_changes" else REJECTED
            )
            note = decision.get("note")
            review.note = _one_line(str(note))[:MAX_NOTE] if note else None
            self.library.set_review(name, version_id, review)
            return (
                "applied",
                f"skills.{name}, version {version_id}: {STATES[review.state]}",
            )
        return None

    def _demote(
        self,
        name: str,
        version_id: str,
        review: Review,
        decision: dict[str, Any],
        item: dict[str, Any],
    ) -> tuple[str, str] | None:
        """
        Apply your demotion of a promoted version: record it as revoked, so it runs
        for no one, the agents that kept it included.

        Returns:
            `applied`, or `conflict` when the version isn't the promoted one now, as
            when a later promotion replaced it; None when this demotion was applied
            already
        """
        decision_id = str(decision.get("id"))
        if any(r.get("decision") == decision_id for r in self.library.revoked(name)):
            return None
        current = self.library.promoted(name)
        if current is None or current.version != version_id:
            return (
                "conflict",
                f"skills.{name}, version {version_id}, isn't the promoted version now: "
                + (f"version {current.version} is" if current else "none is"),
            )
        note = decision.get("note")
        note = _one_line(str(note))[:MAX_NOTE] if note else None
        self.library.demote(
            name,
            {
                "item": item["id"],
                "decision": decision_id,
                "decision_hash": decision.get("decision_hash"),
                "by": decision.get("by"),
                "at": decision.get("at"),
                "note": note,
            },
        )
        review.state, review.decision, review.at = DEMOTED, decision_id, now()
        review.note = note
        self.library.set_review(name, version_id, review)
        return (
            "applied",
            f"demoted skills.{name}, version {version_id}: no agent runs it now",
        )


###############################################################################
#
def _facts(
    version: Version,
    current: Version | None,
    reach: Reach,
    saved: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    What a promotion item shows you about a version, and its risk, which follows
    from what the version reaches.

    Returns:
        The facts, the first being what every agent will read; and the item's risk
        and reasons, high when the version writes or reaches what its code doesn't
        show, each reason pointing at the facts it rests on
    """
    manifest = version.manifest
    facts: list[dict[str, Any]] = [
        {"label": "Every agent will read", "value": version.summary},
        {"label": "Signature", "value": str(manifest["signature"])},
        {
            "label": "Reads with",
            "value": ", ".join(reach.reads) if reach.reads else "no stubs",
        },
    ]
    writing: list[dict[str, int]] = []
    unclear: list[dict[str, int]] = []
    if reach.writes:
        writing.append({"fact": len(facts)})
        facts.append(
            {"label": "Writes with", "value": ", ".join(reach.writes), "flag": True}
        )
    if reach.skills:
        facts.append({"label": "Calls", "value": ", ".join(reach.skills)})
    if reach.unknown:
        unclear.append({"fact": len(facts)})
        facts.append(
            {
                "label": "Names stubs the broker doesn't serve",
                "value": ", ".join(reach.unknown),
                "flag": True,
            }
        )
    for finding in reach.unseen:
        unclear.append({"fact": len(facts)})
        facts.append(
            {
                "label": "Reaches what its code doesn't show",
                "value": finding,
                "flag": True,
            }
        )
    imports = [str(m) for m in manifest.get("imports") or []]
    if imports:
        beyond = [m for m in imports if m not in sys.stdlib_module_names]
        facts.append(
            {
                "label": "Imports",
                "value": ", ".join(imports)
                + (
                    f"; beyond the standard library: {', '.join(beyond)}"
                    if beyond
                    else ""
                ),
            }
        )
    ran = saved.get("ran")
    if ran is None:
        facts.append(
            {
                "label": "Ran",
                "value": "no: it was kept from code sent with it, not from a run, so "
                "nothing has shown it works",
                "flag": True,
            }
        )
    else:
        called = ", ".join(
            f"{stub} {_times(n)}"
            for stub, n in sorted(ran["calls"].items())
            if not stub.startswith("maji.")
        )
        seconds = (ran.get("duration_ms") or 0) / 1000
        facts.append(
            {
                "label": "Ran",
                "value": f"the script it came from exited 0 in {seconds:.1f} s "
                f"(run {str(saved.get('run', ''))[:8]})"
                + (f", calling {called}" if called else ", calling no stubs"),
            }
        )
    if current is not None:
        facts.append(
            {"label": "Replaces", "value": f"version {current.version}, promoted now"}
        )
    facts.append(
        {"label": "Version", "value": f"{manifest['id']}, version {version.version}"}
    )

    name = f"skills.{version.name}"
    if writing:
        reason = {
            "code": "trust_changes",
            "basis": "found",
            "text": f"Once promoted, any agent may run {name}, and it writes with "
            + ", ".join(reach.writes),
            "evidence": writing,
        }
    elif unclear:
        reason = {
            "code": "trust_changes",
            "basis": "suspected",
            "text": f"Once promoted, any agent may run {name}, and what it reaches "
            "can't all be read from its code, so it may write",
            "evidence": unclear[:10],
        }
    else:
        return facts, {"risk": "medium"}
    return facts, {"risk": "high", "risk_reasons": [reason]}


def _times(n: int) -> str:
    return "once" if n == 1 else "twice" if n == 2 else f"{n} times"


def _diff(old: str, new: str) -> list[dict[str, str]]:
    """The new code as diff lines against the old, every line shown."""
    before, after = old.splitlines(), new.splitlines()
    lines: list[dict[str, str]] = []
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for op, a1, a2, b1, b2 in matcher.get_opcodes():
        if op == "equal":
            lines += [{"op": "context", "text": t} for t in before[a1:a2]]
            continue
        lines += [{"op": "delete", "text": t} for t in before[a1:a2]]
        lines += [{"op": "add", "text": t} for t in after[b1:b2]]
    return lines


def _one_line(text: str) -> str:
    """
    Text every agent will read, as one line of printable characters: its whitespace
    run together, and control characters, such as a terminal's escapes, left out.
    """
    printable = "".join(c if c.isprintable() else " " for c in text)
    return " ".join(printable.split())


def _text(params: dict[str, Any], key: str, limit: int) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MajiError("invalid_argument", f"{key} is required")
    if len(value.encode()) > limit:
        raise MajiError("invalid_argument", f"{key} is limited to {limit} bytes")
    return value


def _optional_text(params: dict[str, Any], key: str, limit: int) -> str | None:
    return _text(params, key, limit) if params.get(key) is not None else None
