"""
Tests for the `org` record stubs, which parse emacs-org-mcp's text.

The listings below follow emacs-org-mcp's own format: an index line per task, then the
task's org entry indented by eight spaces, and a footer naming the next offset while
more remain. `tests/local/test_emacs_org_bridge.py` checks the same parsing against the
real server.
"""

# system imports
from typing import Any

# 3rd party imports
import pytest
from faker import Faker
from pytest_check import check

# project imports
from maji_mcp.broker import emacs_org
from maji_mcp.broker.catalog import CallContext, Stub
from maji_mcp.broker.mcp_servers import McpServer, ServerConfig
from maji_mcp.common.protocol import MajiError

FIRST_PAGE = """\
Completed Tasks -- 3 results, showing 1-2
=========================================

   1. DONE [DEMO-1] [[https://t.example/DEMO-1][DEMO-1]] Paint the shed before the r… (#task-one) 3d [12L 0.5k]
        ** DONE [[https://t.example/DEMO-1][DEMO-1]] Paint the shed before the rain
        :PROPERTIES:
           :ID:       8BF9663E
           :CUSTOM_ID: task-one
           :CREATED:  <2026-11-02 Mon 09:30>
           :CLOSED:   <2026-11-04 Wed 08:57>
           :PROJECT:  project-demo
        :END:
        *** Description
        Two coats, front and back.

        Finished on a dry day.
        *** Task items [2/3]
        - [X] Buy the paint
        - [ ] Clean the brushes, which takes
          longer than it should
          - [X] a nested item, counted too
   2. DONE [DEMO-2] Second task (#task-two) 2w [6L 0.2k]
        ** DONE [DEMO-2] Second task
        :PROPERTIES:
           :CUSTOM_ID: task-two
           :CLOSED:   [2026-10-20 Tue]
        :END:

1 more. Next: call list_tasks with offset=2
"""

SECOND_PAGE = """\
Completed Tasks -- 3 results, showing 3-3
=========================================

   3. Note without a keyword (#task-three) 1d [4L 0.1k]
        ** Note without a keyword
        :PROPERTIES:
           :CUSTOM_ID: task-three
        :END:
"""

GET_TASK = """\
DONE  [DEMO-2] Second task
Section: Completed Tasks

** DONE [DEMO-2] Second task
:PROPERTIES:
   :CUSTOM_ID: task-two
   :CLOSED:   [2026-10-20 Tue]
:END:
*** Task items [0/1]
- [ ] the last item
"""


class FakeOrgServer(McpServer):
    """
    An emacs-org-mcp that answers from the listings above, recording each call.

    Args:
        tools: the tools its configuration lists, each as `read` or `write`
    """

    def __init__(self, tools: dict[str, str]) -> None:
        super().__init__(ServerConfig("org", "http://unused", "t", tools))
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool: str, args: dict[str, Any], ctx: CallContext) -> Any:
        """Answer `list_tasks` with a page of the listing, and `get_task` for one id."""
        self.calls.append((tool, args))
        if tool == "list_tasks":
            return FIRST_PAGE if args["offset"] == 0 else SECOND_PAGE
        if args["identifier"] == "task-two":
            return GET_TASK
        return "No task found matching: " + args["identifier"]


@pytest.fixture
def server() -> FakeOrgServer:
    """An emacs-org-mcp configured with both of the adapter's tools listed as reads."""
    return FakeOrgServer({"list_tasks": "read", "get_task": "read"})


@pytest.fixture
def stubs(server: FakeOrgServer) -> dict[str, Stub]:
    """The adapter's stubs for `server`, by name."""
    return {stub.name: stub for stub in emacs_org.stubs(server)}


@pytest.fixture
def ctx(faker: Faker) -> CallContext:
    """Who is calling, which the adapter passes on and the fake server ignores."""
    return CallContext(faker.user_name(), faker.uuid4())


async def test_tasks_parses_every_page_into_records(
    server: FakeOrgServer, stubs: dict[str, Stub], ctx: CallContext
) -> None:
    """
    GIVEN: A section listing that spans two pages
     WHEN: a script calls org.tasks for that section with a property filter
     THEN: it pages until the server names no next offset, passing the filter on each
           page, and returns one record per task with the drawer's fields, ISO
           timestamps, the ticket, and checklist counts that include nested items
    """
    tasks = stubs["org.tasks"]
    records = await tasks.handler(
        tasks.bind(
            {"section": "Completed Tasks", "where": {"PROJECT": "project-demo"}}
        ),
        ctx,
    )

    # It asked for the page the server named next, with the filter each time.
    #
    with check:
        assert [(tool, args["offset"]) for tool, args in server.calls] == [
            ("list_tasks", 0),
            ("list_tasks", 2),
        ]
    with check:
        assert all(
            args["where"] == {"PROJECT": "project-demo"} for _, args in server.calls
        )

    # One record per task, across both pages. Each is compared on the fields its
    # listing sets, so a failure shows every field of that record that differs, and
    # the other records are still compared.
    #
    assert [r["custom_id"] for r in records] == ["task-one", "task-two", "task-three"]
    parsed: list[dict[str, Any]] = [
        {
            "status": "DONE",
            "ticket": "DEMO-1",
            "project": "project-demo",
            "created": "2026-11-02T09:30",
            "closed": "2026-11-04T08:57",
            "sections": ["Description", "Task items"],
            "checklist_open": 1,
            "checklist_done": 2,
            "open_items": ["Clean the brushes, which takes"],
        },
        {"ticket": "DEMO-2", "closed": "2026-10-20"},
        {"status": None, "headline": "Note without a keyword"},
    ]
    for record, fields in zip(records, parsed, strict=True):
        with check:
            assert {name: record[name] for name in fields} == fields
    with check:
        assert records[0]["headline"].endswith("Paint the shed before the rain")


async def test_task_returns_the_record_and_its_exact_org_text(
    stubs: dict[str, Stub], ctx: CallContext
) -> None:
    """
    GIVEN: A task emacs-org-mcp can find
     WHEN: a script calls org.task with its id
     THEN: it gets the parsed record, plus the org entry exactly as get_task gave it,
           without get_task's status and section lines
    """
    task = stubs["org.task"]
    record = await task.handler(task.bind({"id": "task-two"}), ctx)

    with check:
        assert record["custom_id"] == "task-two"
    with check:
        assert record["open_items"] == ["the last item"]
    with check:
        assert record["text"].startswith("** DONE [DEMO-2] Second task\n:PROPERTIES:")
    with check:
        assert record["text"].endswith("- [ ] the last item")


async def test_an_unknown_task_is_not_found(
    stubs: dict[str, Stub], ctx: CallContext
) -> None:
    """
    GIVEN: An id that matches no task
     WHEN: a script calls org.task
     THEN: it raises not_found, carrying the server's message
    """
    task = stubs["org.task"]
    with pytest.raises(MajiError) as raised:
        await task.handler(task.bind({"id": "task-nope"}), ctx)
    assert raised.value.code == "not_found"
    assert "task-nope" in raised.value.message


async def test_an_unknown_section_is_refused_before_any_call(
    server: FakeOrgServer, stubs: dict[str, Stub], ctx: CallContext
) -> None:
    """
    GIVEN: A section name tasks.org doesn't have
     WHEN: a script calls org.tasks with it
     THEN: it raises invalid_argument naming the sections, and nothing reaches the
           server
    """
    tasks = stubs["org.tasks"]
    with pytest.raises(MajiError) as raised:
        await tasks.handler(tasks.bind({"section": "Done"}), ctx)
    assert raised.value.code == "invalid_argument"
    assert "Completed Tasks" in raised.value.message
    assert server.calls == []


@pytest.mark.parametrize(
    "tools",
    [
        {"get_task": "read"},
        {"list_tasks": "read", "get_task": "write"},
        {},
    ],
    ids=["one read missing", "one listed as a write", "neither listed"],
)
def test_the_adapter_needs_both_reads_listed(tools: dict[str, str]) -> None:
    """
    GIVEN: A server configuration that doesn't list both list_tasks and get_task as
           reads
     WHEN: the broker builds the emacs-org adapter's stubs
     THEN: there are none, so the adapter never calls a tool the configuration
           doesn't expose as a read
    """
    assert emacs_org.stubs(FakeOrgServer(tools)) == []


def test_the_record_stubs_are_reads_in_the_servers_namespace(
    stubs: dict[str, Stub],
) -> None:
    """
    GIVEN: A server configured under the `org` namespace, with both reads listed
     WHEN: the broker builds the adapter's stubs
     THEN: they are org.tasks and org.task, neither writes, and each documents the
           record's fields
    """
    with check:
        assert sorted(stubs) == ["org.task", "org.tasks"]
    with check:
        assert not any(stub.writes for stub in stubs.values())
    with check:
        assert stubs["org.tasks"].signature() == (
            "org.tasks(*, section: str = 'Tasks', where: dict = None) -> list[dict]"
        )
    with check:
        assert all("checklist_open" in stub.doc() for stub in stubs.values())
