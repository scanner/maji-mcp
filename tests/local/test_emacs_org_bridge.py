"""
A smoke test of the bridge in front of the real emacs-org-mcp.

It runs your emacs-org-mcp checkout against a small made-up org directory in a
temporary folder, never `~/org`. emacs-org-mcp ignores its environment variables when
started without flags (its docopt defaults win), so everything is passed as a flag.

The `org` record stubs are checked against `tests/local/org_reference.py`, which reads
the same directory through emacs-org-mcp's own parser. `tests/local/org_skills.py`, the
code the first write skills start from, changes a task through the same stubs.

    make test-local
"""

# system imports
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

# 3rd party imports
import pytest
from pytest_check import check

# project imports
from maji_mcp.broker import emacs_org
from maji_mcp.broker.catalog import CallContext, Stub
from maji_mcp.broker.mcp_servers import McpServer, ServerConfig, stubs
from tests.local import org_skills
from tests.support.bridge import serving_bridge
from tests.support.paths import REPO

pytestmark = pytest.mark.local

EMACS_ORG_MCP = Path.home() / "src" / "emacs-org-mcp"

# Two open tasks and a finished one: checklists flat and nested, a ticket in a
# heading, and a CLOSED date, which between them use every field the record stubs
# return.
#
TASKS = """\
* High Level Tasks (in order)
* Tasks
** TODO Demo task for the bridge
:PROPERTIES:
   :CUSTOM_ID: task-demo
   :PROJECT:  project-demo
:END:
*** Description
A made-up task.
*** Task items [0/2]
- [ ] first item
- [ ] second item
** TODO [DEMO-7] Another task, with nested items
:PROPERTIES:
   :CUSTOM_ID: task-nested
   :PROJECT:  project-demo
:END:
*** Task items [1/2]
- [X] done item
- [ ] open item
  - [ ] nested open item
  - [X] nested done item
* Completed Tasks
** DONE A finished task
:PROPERTIES:
   :CUSTOM_ID: task-finished
   :CLOSED:   <2026-10-20 Tue 16:32>
   :PROJECT:  project-demo
:END:
*** Task items [1/1]
- [X] the only item
"""


@pytest.fixture
def org(tmp_path: Path) -> Path:
    """A made-up org directory holding `TASKS`, in the test's temporary folder."""
    org = tmp_path / "org"
    (org / "journal").mkdir(parents=True)
    (org / "projects").mkdir()
    (org / "tasks.org").write_text(TASKS)
    return org


@pytest.fixture
async def org_stubs(
    org: Path, tmp_path: Path, unused_tcp_port: int
) -> AsyncIterator[dict[str, Stub]]:
    """
    The broker's `org` stubs by name, reading `org` through the bridge in front of
    your emacs-org-mcp checkout. Without a checkout, the test is skipped.
    """
    if not (EMACS_ORG_MCP / "server.py").exists() or not shutil.which("uv"):
        pytest.skip("no emacs-org-mcp checkout")
    command = [
        "uv",
        "--directory",
        str(EMACS_ORG_MCP),
        "run",
        "server.py",
        f"--org-dir={org}",
        "--no-ediff-approval",
        "--no-git-autocommit",
    ]
    serving = serving_bridge("emacs-org", command, unused_tcp_port, tmp_path, within=60)
    async with serving as bridge:
        tools = {"get_task": "read", "list_tasks": "read", "update_task": "write"}
        server = McpServer(ServerConfig("org", bridge.url, bridge.token, tools))
        found = {stub.name: stub for stub in await stubs(server)}
        found.update({stub.name: stub for stub in emacs_org.stubs(server)})
        yield found


def parser_records(org: Path, section: str) -> dict[str, dict[str, Any]]:
    """
    Return a section's tasks as emacs-org-mcp's own parser reads them, by CUSTOM_ID.

    The parser runs in a process of its own, inside emacs-org-mcp's environment,
    where its dependencies resolve (`tests/local/org_reference.py`).
    """
    proc = subprocess.run(
        [
            "uv",
            "run",
            "--quiet",
            "--project",
            str(EMACS_ORG_MCP),
            "python",
            str(REPO / "tests" / "local" / "org_reference.py"),
            str(EMACS_ORG_MCP),
            str(org),
            section,
        ],
        capture_output=True,
        text=True,
        check=True,
        env={k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"},
    )
    return {r["custom_id"]: r for r in json.loads(proc.stdout)}


async def test_the_broker_reads_emacs_org_through_the_bridge(
    org: Path, org_stubs: dict[str, Stub]
) -> None:
    """
    GIVEN: emacs-org-mcp, started by the bridge on a made-up org directory
     WHEN: the broker builds stubs for its tools, and calls the read ones
     THEN: org.get_task returns emacs-org-mcp's text for the task, including its
           entry, and org.update_task is marked as a stub that writes
      AND: org.tasks returns the same tasks as emacs-org-mcp's own parser reads, in
           each section, agreeing on each one's status, project, checklist counts
           and closing day
      AND: org.task finds a task by the ticket in its heading
      AND: nothing in the org directory is changed
    """
    ctx = CallContext("reader", "run-1")

    # The stubs made from emacs-org-mcp's own tool list.
    #
    with check:
        assert org_stubs["org.update_task"].writes is True
    text = await org_stubs["org.get_task"].handler({"identifier": "task-demo"}, ctx)
    with check:
        assert "Demo task for the bridge" in text
    with check:
        assert "- [ ] first item" in text

    # The record stubs, which must agree with emacs-org-mcp's own parser on every
    # field compared here. Each field of each record is checked on its own, so one
    # run shows every disagreement.
    #
    for section, name in (("active", "Tasks"), ("completed", "Completed Tasks")):
        expected = parser_records(org, section)
        records = await org_stubs["org.tasks"].handler(
            {"section": name, "where": None}, ctx
        )
        got = {r["custom_id"]: r for r in records}
        assert sorted(got) == sorted(expected)
        for custom_id, record in got.items():
            truth = expected[custom_id]
            with check:
                assert record["status"] == truth["status"], custom_id
            with check:
                assert record["project"] == (truth["project"] or None), custom_id
            with check:
                assert record["checklist_open"] == truth["checklist_open"], custom_id
            with check:
                assert record["checklist_done"] == truth["checklist_done"], custom_id
            with check:
                assert (record["closed"] or "")[:10] == (
                    emacs_org.iso(truth["closed"]) or ""
                )[:10], custom_id

    # One record, found by the ticket in its heading.
    #
    record = await org_stubs["org.task"].handler({"id": "DEMO-7"}, ctx)
    with check:
        assert record["custom_id"] == "task-nested"
    with check:
        assert record["ticket"] == "DEMO-7"
    with check:
        assert (record["checklist_open"], record["checklist_done"]) == (2, 2)

    with check:
        assert (org / "tasks.org").read_text() == TASKS


async def test_a_checklist_item_is_checked_off_through_the_bridge(
    org: Path, org_stubs: dict[str, Stub]
) -> None:
    """
    GIVEN: emacs-org-mcp, started by the bridge on a made-up org directory holding a
           task with two open checklist items under a heading that counts them
     WHEN: complete_task_item is given the task and part of one item's text
     THEN: a dry run returns the two lines that would change, and changes nothing
      AND: the real run checks that item in the org file, and the heading counts 1
           of 2
      AND: the task's other item, and every other task, are as they were
    """
    ctx = CallContext("writer", "run-1")

    async def call(tool: str, arguments: dict[str, Any]) -> str:
        return cast(str, await org_stubs[f"org.{tool}"].handler(arguments, ctx))

    diff = await org_skills.complete_task_item(call, "task-demo", "first", dry_run=True)
    with check:
        assert "+- [X] first item" in diff
    with check:
        assert "+*** Task items [1/2]" in diff
    assert (org / "tasks.org").read_text() == TASKS

    said = await org_skills.complete_task_item(call, "task-demo", "first")
    with check:
        assert said == "checked off 1 item in task-demo; Task items [1/2]"

    # emacs-org-mcp stamps a task it saves with `:MODIFIED:`, so that line is set
    # aside, and the rest of the file must differ from `TASKS` in the item and its
    # heading alone.
    #
    saved = [
        line
        for line in (org / "tasks.org").read_text().splitlines()
        if ":MODIFIED:" not in line
    ]
    expected = TASKS.replace("- [ ] first item", "- [X] first item").replace(
        "*** Task items [0/2]", "*** Task items [1/2]"
    )
    assert saved == expected.splitlines()
