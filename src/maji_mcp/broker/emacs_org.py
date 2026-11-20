"""
Stubs that turn emacs-org-mcp's text into records a script can filter and count.

emacs-org-mcp answers in text written for a model to read. A script wants fields: which
tasks belong to a project, how many checklist items are open, when a task was closed.
These stubs call the server's own read tools and parse what they return, so the server
stays as it is (`docs/decisions.md`, "Existing MCP servers are used as they are").

`list_tasks` with `detail="full"` prints each task as an index line, then its exact org
entry indented by eight spaces:

       1. DONE Paint the shed before the r… (#task-x) 3d [28L 1.4k]
            ** DONE Paint the shed before the rain comes
            :PROPERTIES:
               :CUSTOM_ID: task-x
               :CLOSED:   <2026-11-04 Wed 08:57>
            :END:
            *** Task items [2/2]
            - [X] Buy the paint

and ends with `N more. Next: call list_tasks with offset=K` while more remain. The org
entry is parsed, not the index line, whose headline is cut short.

The stubs are added for a server whose configuration says `"adapter": "emacs-org"`,
and only when the configuration lists `list_tasks` and `get_task` as reads, so the
adapter never reaches a tool the configuration doesn't expose.
"""

# system imports
import re
from typing import Any

# project imports
from maji_mcp.broker.catalog import CallContext, Param, Stub
from maji_mcp.broker.mcp_servers import McpServer
from maji_mcp.common.protocol import MajiError

SECTIONS = ("Tasks", "Completed Tasks")
KEYWORDS = {
    "TODO",
    "DONE",
    "WAITING",
    "NEXT",
    "STARTED",
    "HOLD",
    "CANCELLED",
    "CANCELED",
}
PAGE = 100
MAX_PAGES = 20
INDENT = " " * 8

INDEX_LINE = re.compile(r"^\s{1,7}\d+\. ")
NEXT_OFFSET = re.compile(r"Next: call list_tasks with offset=(\d+)")
HEADLINE = re.compile(r"^\*\*\s+(.*)$")
PROPERTY = re.compile(r"^\s*:([A-Za-z0-9_-]+):\s*(.*?)\s*$")
TIMESTAMP = re.compile(r"[<\[](\d{4}-\d\d-\d\d)(?: [A-Za-z]{2,3})?(?: (\d\d:\d\d))?")
LINKED_TICKET = re.compile(r"^\[\[[^\]]*\]\[([A-Z][A-Z0-9]+-\d+)\]\]")
BARE_TICKET = re.compile(r"^\[([A-Z][A-Z0-9]+-\d+)\]")
CHECKBOX = re.compile(r"^- \[([ Xx-])\] ?(.*)$")
SUBSECTION = re.compile(r"^\*\*\*\s+(.*?)\s*(?:\[\d*/\d*\]|\[\d*%\])?\s*$")


###############################################################################
#
def iso(value: str | None) -> str | None:
    """Return an org timestamp as ISO 8601 (`2026-11-04T08:57`), or None."""
    match = TIMESTAMP.search(value or "")
    if not match:
        return None
    day, time = match.groups()
    return f"{day}T{time}" if time else day


def parse_entry(text: str) -> dict[str, Any]:
    """
    Parse one task's org entry into a record.

    Checklist items are counted on every line that starts with a checkbox, nested
    items included, as `tests/local/org_reference.py` counts them.

    Returns:
        custom_id, status, headline, ticket, project, closed, created, modified,
        properties, sections, checklist_open, checklist_done, open_items, size
    """
    lines = text.splitlines()
    head = HEADLINE.match(lines[0]) if lines else None
    if head is None:
        raise MajiError("internal", "an org entry didn't start with a ** headline")
    words = head.group(1).split(" ", 1)
    status = words[0] if words[0] in KEYWORDS else None
    headline = (words[1] if status and len(words) > 1 else head.group(1)).strip()
    ticket = LINKED_TICKET.match(headline) or BARE_TICKET.match(headline)

    properties: dict[str, str] = {}
    in_drawer = False
    sections: list[str] = []
    open_items: list[str] = []
    done = 0
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == ":PROPERTIES:":
            in_drawer = True
            continue
        if in_drawer:
            if stripped == ":END:":
                in_drawer = False
            elif match := PROPERTY.match(line):
                properties[match.group(1).upper()] = match.group(2)
            continue
        if match := SUBSECTION.match(line):
            sections.append(match.group(1))
        elif match := CHECKBOX.match(line.lstrip()):
            if match.group(1) == " ":
                open_items.append(match.group(2))
            elif match.group(1) in "Xx":
                done += 1
    return {
        "custom_id": properties.get("CUSTOM_ID"),
        "status": status,
        "headline": headline,
        "ticket": ticket.group(1) if ticket else None,
        "project": properties.get("PROJECT"),
        "closed": iso(properties.get("CLOSED")),
        "created": iso(properties.get("CREATED")),
        "modified": iso(properties.get("MODIFIED")),
        "properties": properties,
        "sections": sections,
        "checklist_open": len(open_items),
        "checklist_done": done,
        "open_items": open_items,
        "size": len(text),
    }


def split_listing(text: str) -> tuple[list[str], int | None]:
    """
    Split a `detail="full"` listing into each task's org entry.

    Returns:
        The entries, in order, and the offset of the next page, or None on the last
    """
    entries: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if INDEX_LINE.match(line):
            current = []
            entries.append(current)
        elif current is not None and (line.startswith(INDENT) or not line.strip()):
            current.append(line[len(INDENT) :])
        else:
            current = None
    found = NEXT_OFFSET.search(text)
    return (
        ["\n".join(entry).rstrip("\n") for entry in entries if entry],
        int(found.group(1)) if found else None,
    )


def entry_of_get_task(text: str) -> str:
    """
    Return the org entry from `get_task`'s answer.

    `get_task` prints a status line, a `Section:` line and a blank line before the
    entry; anything else, such as a list of candidates, has no entry to return.

    Raises:
        MajiError: `not_found`, with the server's text, when there is no entry
    """
    lines = text.splitlines()
    for n, line in enumerate(lines):
        if HEADLINE.match(line):
            return "\n".join(lines[n:]).rstrip("\n")
    raise MajiError("not_found", text.strip()[:2000])


###############################################################################
#
def stubs(server: McpServer) -> list[Stub]:
    """
    Return the record stubs for an emacs-org-mcp server, in its namespace.

    Returns:
        No stubs unless the configuration lists `list_tasks` and `get_task` as reads.
    """
    tools = server.config.tools
    if tools.get("list_tasks") != "read" or tools.get("get_task") != "read":
        return []
    ns = server.config.namespace

    async def tasks(args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        if args["section"] not in SECTIONS:
            raise MajiError(
                "invalid_argument", f"section must be one of {', '.join(SECTIONS)}"
            )
        call: dict[str, Any] = {"section": args["section"], "detail": "full"}
        if args["where"]:
            call["where"] = args["where"]
        records: list[dict[str, Any]] = []
        offset: int | None = 0
        for _ in range(MAX_PAGES):
            text = await server.call(
                "list_tasks", {**call, "limit": PAGE, "offset": offset}, ctx
            )
            entries, offset = split_listing(str(text))
            records += [parse_entry(entry) for entry in entries]
            if offset is None:
                return records
        raise MajiError(
            "internal", f"more than {PAGE * MAX_PAGES} tasks; narrow with where"
        )

    async def task(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        text = str(await server.call("get_task", {"identifier": args["id"]}, ctx))
        entry = entry_of_get_task(text)
        return {**parse_entry(entry), "text": entry}

    record = (
        "custom_id, status (TODO, DONE, ...), headline, ticket, project, closed, "
        "created and modified (ISO 8601, or None), properties (the drawer), sections "
        "(subheading names), checklist_open and checklist_done (every checkbox line, "
        "nested ones included), open_items (each open item's first line), size"
    )
    return [
        Stub(
            f"{ns}.tasks",
            "Tasks in one section of tasks.org, as records.",
            {
                "section": Param(
                    "str", "`Tasks` (active) or `Completed Tasks`", "Tasks"
                ),
                "where": Param(
                    "dict",
                    "exact, case-insensitive matches on drawer properties, such as "
                    '{"PROJECT": "project-x"}, plus the pseudo-fields status and section',
                    None,
                ),
            },
            f"list[dict]: one per task, with {record}",
            tasks,
            notes=f"Parsed from list_tasks; prefer it to {ns}.list_tasks in a script.",
        ),
        Stub(
            f"{ns}.task",
            "One task, as a record with its org text.",
            {"id": Param("str", "a CUSTOM_ID, ticket id or headline substring")},
            f"dict: {record}, and text: the exact org entry",
            task,
            notes="Parsed from get_task, which returns the first task the id matches. "
            "An id that matches none raises not_found.",
        ),
    ]
