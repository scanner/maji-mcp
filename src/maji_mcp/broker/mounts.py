"""
Whether a run may mount the paths it names: the control plane's grants decide, and the
paths not yet granted go to you as one queue item.

The runner has already resolved each path (`runner/mounts.py`), so the paths here are
the ones a grant would cover and an executor would see. For each, the control plane
says whether an active grant covers this principal mounting it in this session. If
every path is covered, the run goes ahead with them. If not, the run doesn't start:
the paths not yet asked about become one item, a checklist with a pattern for each
path, and the runner returns at once, naming the item.

`run_python` returns before you decide, so the agent runs the script again to learn
the answer. A rerun mustn't put the same paths to you again, so the broker keeps, for
each principal, session and path, the last item that asked for it:
- **while that item waits,** the rerun is told about the same item
- **once you've refused the path,** the rerun is refused for the rest of the session
  instead of asking again, until you let it ask again from the item (`let_ask_again`
  in the control plane), as you might once you've told the agent more. A new session
  can ask again. A run with no session (`gateway/server.py`, `session_of`) is
  refused until you let it ask again, or the broker restarts.
- **once the item has lapsed or been withdrawn,** or the grant you gave has lapsed or
  been revoked, the path is asked for again

A grant on a folder covers everything in it, so a path inside a folder already
granted runs without asking (`ControlStore.grant_covers`). A refusal covers only the
path refused: a folder refused as too broad doesn't stop a request for one inside it,
which is what an agent asks for once you've told it where to look.

A mount of a folder leaves out the files in it named the way secrets usually are, and
the places in it on your never-mount list: the runner covers each, so a script reads
it as empty (`runner/mounts.py`). Before a path is put to you, the broker answers the
run with `scan`, the paths to look through, and the runner registers it again with
what a mount of each would leave out. That goes on the item, a fact for each path, so
you see what the grant doesn't give.

A file named like a secret is read only when the agent asks for the file by name, and
then on a grant of that very path: the runner names such paths in `exact`, and a
grant on a folder holding one doesn't cover it. Its item is high risk, with the
reason `secrets_in_reach`.

What it keeps is held in memory: after the broker restarts, a waiting or refused path
can be asked for once more.
"""

# system imports
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# project imports
from maji_mcp.common.names import session_name
from maji_mcp.common.protocol import MajiError, call_unix

MAX_MOUNTS = 10
MAX_WHY = 2000
MAX_NAMED = 20
MAX_NAME = 1000
MAX_CLIENT_TEXT = 100


###############################################################################
#
@dataclass(frozen=True)
class Asked:
    """An item the broker raised for a path, and that path's part in it."""

    item: str
    part: str


class MountGrants:
    """
    Asks the control plane whether a run's paths are granted, and raises an item for
    those that aren't.

    Args:
        control_plane: the control plane's socket for the broker, or None for a
            broker without one, which mounts nothing
    """

    def __init__(self, control_plane: Path | None) -> None:
        self.control_plane = control_plane
        self._asked: dict[tuple[str, str | None, str], Asked] = {}
        self._raising = asyncio.Lock()

    async def check(
        self,
        principal: str,
        session: str | None,
        paths: list[str],
        why: str | None = None,
        findings: dict[str, Any] | None = None,
        client: dict[str, str] | None = None,
        exact: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """
        Decide whether a run may mount `paths` now.

        Args:
            principal: who the run is for
            session: the session key the gateway derived, if the agent keeps one
            paths: resolved absolute paths, as the runner resolved them
            why: the agent's words on why the script needs them, kept as the item's
                claim
            findings: what the runner found a mount of each path it was asked to
                look through would leave out, by path (`runner/mounts.py`)
            client: what the agent's app said it is, shown on the item as its own
                account
            exact: the paths among them that are files named like secrets. Each
                needs a grant on that very path: a grant on a folder holding it
                covers a mount of the folder, which leaves the file out.

        Returns:
            None when every path is granted. Otherwise `scan`, the paths to look
            through before they are put to you, when any lacks findings; or
            `pending`, what the run waits on: `items`, the queue items to decide, and
            `paths`, the paths not granted.

        Raises:
            MajiError: `permission_denied` for a path you refused in this session and
                haven't let it ask for again, and `unavailable` when there is no
                control plane to ask
        """
        if not paths:
            return None
        if self.control_plane is None:
            raise MajiError(
                "unavailable", "this broker has no control plane to grant mounts"
            )
        alone = set(exact or [])
        ungranted = [
            p
            for p in paths
            if not await self._granted(principal, session, p, exact=p in alone)
        ]
        if not ungranted:
            return None

        waiting: list[str] = []
        new: list[str] = []
        seen: dict[str, Asked | None] = {}
        for path in ungranted:
            asked = seen[path] = self._asked.get((principal, session, path))
            state = await self._state(asked, path, session) if asked else "ask"
            if state == "waiting":
                assert asked is not None
                if asked.item not in waiting:
                    waiting.append(asked.item)
            else:
                new.append(path)
        if new:
            unseen = [p for p in new if p not in (findings or {})]
            if unseen:
                return {"scan": unseen}
            looked = checked_findings(findings or {}, paths)

            # Another run asking for the same paths may have raised an item while
            # this one was being looked through: a path whose last item changed since
            # it was read above waits on that item instead of a second one.
            #
            async with self._raising:
                fresh = []
                for path in new:
                    asked = self._asked.get((principal, session, path))
                    if asked is seen[path]:
                        fresh.append(path)
                    elif asked is not None and asked.item not in waiting:
                        waiting.append(asked.item)
                if fresh:
                    waiting.append(
                        await self._raise(
                            principal, session, fresh, why, looked, client
                        )
                    )
        return {"pending": {"items": waiting, "paths": ungranted}}

    async def _granted(
        self, principal: str, session: str | None, path: str, *, exact: bool = False
    ) -> bool:
        params: dict[str, Any] = {
            "principal": principal,
            "access": "mount",
            "resource": path,
        }
        if exact:
            params["exact"] = True
        if session:
            params["session"] = session
        found = await self._call("grant_covers", params)
        return found.get("grant") is not None

    async def _state(self, asked: Asked, path: str, session: str | None) -> str:
        """
        Say what the last item asking for `path` means for a rerun: `waiting`, or
        `ask` to ask again.

        Raises:
            MajiError: `permission_denied` when you refused the path in it, and
                haven't since let it be asked for again
        """
        status = await self._call("status_of", {"item": asked.item})
        if status["state"] == "waiting":
            return "waiting"
        decision = status.get("decision") or {}
        refused = status["state"] == "decided" and (
            decision.get("action") != "approve"
            or (decision.get("parts") or {}).get(asked.part, "approve") != "approve"
        )
        if refused and not status.get("ask_again"):
            during = f" in this session, {session_name(session)}," if session else ""
            raise MajiError(
                "permission_denied",
                f"the user refused {path} in {asked.item}, so it isn't asked for "
                f"again{during} unless they let it be, from that item",
                {"item": asked.item, "path": path},
            )
        return "ask"

    async def _raise(
        self,
        principal: str,
        session: str | None,
        paths: list[str],
        why: str | None,
        looked: dict[str, dict[str, Any]],
        client: dict[str, str] | None = None,
    ) -> str:
        """
        Put the paths to you as one item, a part and a pattern for each, saying what
        a mount of each leaves out. The item is high risk when a path is itself a
        file named like a secret, which nothing covers.
        """
        parts = [{"id": f"p{n}", "label": path} for n, path in enumerate(paths, 1)]
        requester: dict[str, Any] = {"principal": principal}
        if session:
            requester["session"] = session
        if client:
            requester["client"] = client
        facts: list[dict[str, Any]] = [
            {"label": "Access", "value": "read-only, in scripts it runs"},
            {
                "label": "A grant covers",
                "value": "each path and everything in it, including folders in it "
                "asked for later, apart from what is left out: files named like "
                "secrets, and places on your never-mount list. A script reads each "
                "of those as empty.",
            },
        ]
        evidence = []
        for path in paths:
            fact = _fact(path, looked[path])
            if fact is None:
                continue
            if looked[path]["itself"]:
                evidence.append({"fact": len(facts)})
            facts.append(fact)
        risk: dict[str, Any] = {}
        if evidence:
            count = len(evidence)
            risk = {
                "risk": "high",
                "risk_reasons": [
                    {
                        "code": "secrets_in_reach",
                        "basis": "suspected",
                        "text": f"{count} of these {'is a file' if count == 1 else 'are files'} "
                        "named the way secrets are kept, asked for by name, and a "
                        "script could read what this mounts",
                        "evidence": evidence,
                    }
                ],
            }
        item = await self._call(
            "create_item",
            {
                "kind": "grant",
                "title": (
                    f"Mount {paths[0]} read-only"
                    if len(paths) == 1
                    else f"Mount {len(paths)} paths read-only"
                ),
                "requester": requester,
                "facts": facts,
                **risk,
                "blocking": True,
                "claim": why,
                "parts": parts,
                "patterns": [
                    {
                        "id": f"mount-{part['id']}",
                        "summary": f"Read-only mounts of {part['label']}",
                        "access": "mount",
                        "resource": part["label"],
                        "match": {"path": part["label"]},
                        "parts": [part["id"]],
                    }
                    for part in parts
                ],
            },
        )
        for part in parts:
            self._asked[(principal, session, part["label"])] = Asked(
                item["id"], part["id"]
            )
        return str(item["id"])

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.control_plane is not None
        try:
            result: dict[str, Any] = await call_unix(self.control_plane, method, params)
        except OSError:
            raise MajiError(
                "unavailable", "the control plane can't be reached to grant mounts"
            ) from None
        return result


def _fact(path: str, found: dict[str, Any]) -> dict[str, Any] | None:
    """
    Say what a mount of a path leaves out, or that the path is itself a file named
    like a secret. None for a path that is neither.
    """
    if found["itself"]:
        return {
            "label": "Named like a secret",
            "value": f"{path}, asked for by name: a mount of the folder holding it "
            "leaves it out, and this mounts the file itself",
            "flag": True,
        }
    count, files = found["count"], found["left_out"]
    if not count:
        return None
    named = ", ".join(files)
    if count > len(files):
        named += f", and {count - len(files)} more"
    return {
        "label": f"Left out of {path}",
        "value": f"{named}: each reads as empty to a script",
    }


def checked_findings(value: object, paths: list[str]) -> dict[str, dict[str, Any]]:
    """
    Check what the runner found a mount of each path would leave out.

    Returns:
        Each path's findings: the names left out, how many in all, whether the path
        is itself a file named like a secret, and how many entries were looked at

    Raises:
        MajiError: `invalid_argument` for findings on a path the run didn't ask for,
            or not in that shape
    """
    if not isinstance(value, dict) or not set(value) <= set(paths):
        raise MajiError(
            "invalid_argument", "findings are by the paths the run asks for"
        )
    for found in value.values():
        if (
            not isinstance(found, dict)
            or not isinstance(found.get("left_out"), list)
            or len(found["left_out"]) > MAX_NAMED
            or not all(
                isinstance(f, str) and 0 < len(f) <= MAX_NAME for f in found["left_out"]
            )
            or type(found.get("count")) is not int
            or found["count"] < len(found["left_out"])
            or type(found.get("itself")) is not bool
            or type(found.get("looked_at")) is not int
            or found["looked_at"] < 0
        ):
            raise MajiError(
                "invalid_argument",
                "a finding is left_out, count, itself and looked_at",
            )
    return value


def checked_exact(value: object, paths: list[str]) -> list[str]:
    """
    Check the paths the runner says need a grant of their own: some of the run's own.

    Raises:
        MajiError: `invalid_argument` for anything else
    """
    if value is None:
        return []
    if not isinstance(value, list) or not set(value) <= set(paths):
        raise MajiError("invalid_argument", "exact is a list of paths the run asks for")
    return list(value)


def checked_client(value: object) -> dict[str, str] | None:
    """
    Check what the gateway says the agent's app called itself: a name, and perhaps a
    version, each short text.

    Raises:
        MajiError: `invalid_argument` for anything else
    """
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or not set(value) <= {"name", "version"}
        or "name" not in value
        or not all(
            isinstance(v, str) and 0 < len(v) <= MAX_CLIENT_TEXT for v in value.values()
        )
    ):
        raise MajiError(
            "invalid_argument", "client is a name and a version, each short text"
        )
    return value


def checked_paths(value: object) -> list[str]:
    """
    Check the paths a run asks to mount: absolute, each once, at most `MAX_MOUNTS`.

    Raises:
        MajiError: `invalid_argument` naming what is wrong
    """
    if value is None:
        return []
    if (
        not isinstance(value, list)
        or len(value) > MAX_MOUNTS
        or not all(isinstance(p, str) and p.startswith("/") for p in value)
        or len(set(value)) != len(value)
    ):
        raise MajiError(
            "invalid_argument",
            f"mounts is a list of at most {MAX_MOUNTS} different absolute paths",
        )
    return list(value)
