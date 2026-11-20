"""
Fixtures for the broker's tests: scripts run the way a run's script is,
`skills.keys` at each stage of its life, and the broker serving the stubs of a
service it calls with a key, answered by a pretend service.

The broker itself, session keys, and the broker with a skill library (`skills`) are
in the root `conftest.py`, since other packages' tests use them too.
"""

# system imports
import asyncio
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any

# 3rd party imports
import httpx
import pytest

# project imports
from maji_mcp.broker import github, jira
from maji_mcp.broker.catalog import Catalog
from maji_mcp.broker.policy import Grant, Policy
from maji_mcp.broker.runlog import RunLog
from tests.support.github import GITHUB_API, GITHUB_TOKEN, FakeGitHub
from tests.support.jira import EMAIL, GATEWAY, JIRA_TOKEN, SITE, SITE_URL, FakeJira
from tests.support.services import Services, fixed_key
from tests.support.skills import TRIED, Skills

# What a test hands `run_script`: a function given the stub client, which a script in
# the executor has as `maji`.
#
Script = Callable[[ModuleType], Any]


@pytest.fixture
def run_script(services: Services) -> Callable[..., Awaitable[Any]]:
    """
    A function that runs `script(maji)` as a new run's script would: off the event
    loop, with the stub client holding the run's token. It returns what the script
    returned.
    """

    async def run_script(
        script: Script, principal: str = "reader", session: str | None = None
    ) -> Any:
        _, token = await services.start_run(principal, session)
        return await asyncio.to_thread(script, services.stub_client(token))

    return run_script


@pytest.fixture
def script_error(
    run_script: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """
    A function that runs `script(maji)` as `run_script` does, for a script whose call
    fails, and returns the `StubError` the script was given.
    """

    async def script_error(
        script: Script, principal: str = "reader", session: str | None = None
    ) -> Any:
        def failing(maji: ModuleType) -> Any:
            with pytest.raises(maji.StubError) as raised:
                script(maji)
            return raised.value

        return await run_script(failing, principal, session)

    return script_error


@pytest.fixture
async def tried(skills: Skills) -> str:
    """
    The id of the keeper's run of TRIED: a script that defined `keys`, called it, and
    succeeded.
    """
    run, _ = await skills.run(TRIED)
    return run


@pytest.fixture
async def kept(skills: Skills, tried: str) -> dict[str, Any]:
    """`skills.keys`, kept by the keeper from the run in `tried`: save_skill's reply."""
    return await skills.keep(run=tried)


@pytest.fixture
async def waiting(skills: Skills, kept: dict[str, Any]) -> dict[str, Any]:
    """
    `skills.keys` waiting for you to promote it. The value is promote_skill's reply,
    which names the item.
    """
    return await skills.promote()


@pytest.fixture
async def promoted(skills: Skills, waiting: dict[str, Any]) -> dict[str, Any]:
    """
    `skills.keys` promoted: you promoted its item, and the broker applied the
    decision. The value is promote_skill's reply, which names the item.
    """
    await skills.decide(waiting["item"], "promote")
    return waiting


def _serving(services: Services, namespace: str, catalog: Catalog) -> None:
    """Make the broker serve `catalog` alone, to a principal allowed `namespace`."""
    services.broker.catalog = catalog
    services.broker.policy = Policy({"reader": Grant(allow=(f"{namespace}.*",))})
    services.broker.runlog.db.close()
    services.broker.runlog = RunLog(":memory:")


@pytest.fixture
def fake_jira(services: Services) -> FakeJira:
    """The broker with the `jira` stubs alone, answered by a pretend Jira."""
    fake = FakeJira()
    client = jira.JiraClient(
        fixed_key(JIRA_TOKEN, site=SITE, email=EMAIL),
        GATEWAY,
        SITE_URL,
        transport=httpx.MockTransport(fake),
    )
    catalog = Catalog()
    catalog.add(jira.VERSION, *jira.stubs(client))
    _serving(services, "jira", catalog)
    return fake


@pytest.fixture
def fake_github(services: Services) -> FakeGitHub:
    """The broker with the `github` stubs alone, answered by a pretend GitHub."""
    fake = FakeGitHub()
    client = github.GitHubClient(
        GITHUB_API, fixed_key(GITHUB_TOKEN), transport=httpx.MockTransport(fake)
    )
    catalog = Catalog()
    catalog.add(github.VERSION, *github.stubs(client))
    _serving(services, "github", catalog)
    return fake
