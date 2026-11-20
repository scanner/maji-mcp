"""
What the tests drive skills with: a script that tries a function, as an agent writes
one, and the broker with a skill library, the control plane behind it, and its feed.
"""

# system imports
import asyncio
import contextlib
import hashlib
import io
from typing import Any

# project imports
from maji_mcp.broker.decisions import DecisionFeed
from maji_mcp.broker.library import Library
from maji_mcp.control.store import ControlStore
from tests.support.services import Services

# A script that tries a function that reads: it defines `keys`, calls it and prints
# what it returned. SUMMARY is the summary `keys` is kept with unless a test gives one.
#
TRIED = '''
def keys(limit: int = 2) -> list:
    """The keys of the first few things, as fake.list gives them back."""
    return sorted(fake.list(limit=limit)["args"])


print(keys())
'''
SUMMARY = "Sorted keys of fake.list's arguments."


class Skills:
    """
    The broker with a skill library, the control plane behind it, and its feed.

    Args:
        services: the running broker
        store: the control plane's store, where promotion items wait and are decided
        library: the broker's skill library
        feed: the broker's reader of your decisions
        session: the session a script runs in, and a skill is kept or promoted from,
            unless the call names another
    """

    def __init__(
        self,
        services: Services,
        store: ControlStore,
        library: Library,
        feed: DecisionFeed,
        session: str,
    ) -> None:
        self.services = services
        self.store = store
        self.library = library
        self.feed = feed
        self.session = session

    async def run(
        self, code: str, session: str | None = None, principal: str = "keeper"
    ) -> tuple[str, str]:
        """
        Run a script as the runner would, logged with its hash and how it ended.

        Returns:
            The run's id, and what it printed, or the error it raised as
            `code: message`
        """
        registered = await self.services.control(
            "register_run",
            principal=principal,
            runtime="python",
            script_sha256=hashlib.sha256(code.encode()).hexdigest(),
            session=session or self.session,
        )
        client = self.services.stub_client(registered["token"])

        def execute() -> str:
            namespace: dict[str, Any] = {
                "__name__": "__main__",
                "maji": client,
                "skills": client.skills,
                **client.namespaces(),
            }
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                exec(compile(code, "<run_python>", "exec"), namespace)
            return out.getvalue().strip()

        try:
            printed, exit_code = await asyncio.to_thread(execute), 0
        except client.StubError as exc:
            printed, exit_code = f"{exc.code}: {exc.message}", 1
        await self.services.control(
            "finish_run",
            run_id=registered["run_id"],
            exit_code=exit_code,
            duration_ms=1200,
        )
        return registered["run_id"], printed

    async def keep(
        self,
        script: str = TRIED,
        run: str | None = None,
        session: str | None = None,
        principal: str = "keeper",
        name: str = "keys",
        summary: str = SUMMARY,
        **more: Any,
    ) -> dict[str, Any]:
        """Keep a function with save_skill: from the run named, or else from code."""
        params: dict[str, Any] = {
            "principal": principal,
            "session": session or self.session,
            "name": name,
            "summary": summary,
            "script": script,
            **more,
        }
        if run:
            params["run"] = run
        saved: dict[str, Any] = await self.services.control("save_skill", **params)
        return saved

    async def try_and_keep(self, script: str, **by: Any) -> dict[str, Any]:
        """Run a script, then keep its function naming that run, as one agent would."""
        ran_as = {key: by[key] for key in ("principal", "session") if key in by}
        run, _ = await self.run(script, **ran_as)
        return await self.keep(script=script, run=run, **by)

    async def promote(
        self,
        name: str = "keys",
        principal: str = "keeper",
        session: str | None = None,
        **more: Any,
    ) -> dict[str, Any]:
        """Ask for a kept skill to be promoted, with promote_skill."""
        asked: dict[str, Any] = await self.services.control(
            "promote_skill",
            principal=principal,
            session=session or self.session,
            name=name,
            **more,
        )
        return asked

    def item(self, item_id: str) -> dict[str, Any]:
        """The item as the control plane holds it now, with any decision on it."""
        found = self.store.get_item(item_id)
        assert found is not None
        return found

    def record(self, item_id: str, action: str, **choice: Any) -> dict[str, Any]:
        """Record your decision on an item, which the broker has yet to read."""
        [decision] = self.store.record_decisions(
            [
                {
                    "item": item_id,
                    "content_hash": self.item(item_id)["content_hash"],
                    "action": action,
                    **choice,
                }
            ],
            {"kind": "you", "passkey": "k"},
        )
        return decision

    async def decide(self, item_id: str, action: str, **choice: Any) -> None:
        """Decide an item as you would, then let the broker apply it."""
        self.record(item_id, action, **choice)
        await self.feed.catch_up()

    async def demote(self, item_id: str, note: str | None = None) -> None:
        """Demote an item's promotion as you would, then let the broker apply it."""
        promoted = self.item(item_id)["decision"]
        await self.decide(
            item_id,
            "demote",
            supersedes=promoted["id"],
            **({"note": note} if note else {}),
        )
