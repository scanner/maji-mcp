"""
A first skill that writes, for org tasks, built over emacs-org-mcp's existing tools.

`complete_task_item` checks off one checklist item in a task. Its work happens in code
over `get_task` and `update_task`, so the task's text never passes through the model:
an agent sends a task's name and a few words of the item, where regenerating the task
sends the whole task back. The first measurements put that at about 80% less for a
10,000-character task (`docs/research/measurements.md`, "Once the code exists").

The broker refuses every write until changesets are built, so this is kept here as
the code the first write skills start from (`docs/design/planned/writes.md`), with a
test that runs it against a real emacs-org-mcp on a made-up org directory
(`test_emacs_org_bridge.py`). `complete_task_item` reaches emacs-org-mcp through the
`call` its caller passes. As a kept skill, the same steps will call the `org` stubs:

    entry = task_entry(org.get_task(identifier=identifier))
    new_entry, heading = tick_item(entry, item_text)
    org.update_task(identifier=identifier, task_entry=new_entry)

`complete_task_item` began as the script an agent wrote to swap two hard-coded lines.
This version finds the item by its text, recomputes the progress cookie, and refuses
any case it can't handle exactly.
"""

# system imports
import difflib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

HEADING_RE = re.compile(r"^\*+ ")
ITEM_RE = re.compile(r"^(?P<indent>\s*)- \[(?P<box>[ Xx-])\] (?P<text>.*)$")
FRACTION_COOKIE_RE = re.compile(r"\[\d*/\d*\]")
PERCENT_COOKIE_RE = re.compile(r"\[\d*%\]")

# How `complete_task_item` reaches emacs-org-mcp: a tool's name and its arguments in,
# the tool's text out.
#
Call = Callable[[str, dict[str, Any]], Awaitable[str]]


###############################################################################
#
@dataclass
class Item:
    """One checklist item: where it starts, and its text joined across lines."""

    line: int
    indent: int
    box: str
    text: str

    @property
    def done(self) -> bool:
        return self.box in "Xx"


###############################################################################
#
def _items(lines: list[str]) -> list[Item]:
    """
    Return every checklist item in an entry.

    A wrapped item continues on lines indented past its `-`, so those lines are joined
    to its text. That way a caller can name an item by any part of it.
    """
    items: list[Item] = []
    current: Item | None = None
    for n, line in enumerate(lines):
        match = ITEM_RE.match(line)
        if match:
            current = Item(
                line=n,
                indent=len(match["indent"]),
                box=match["box"],
                text=match["text"].strip(),
            )
            items.append(current)
        elif (
            current
            and line.strip()
            and len(line) - len(line.lstrip()) > current.indent
            and not HEADING_RE.match(line)
        ):
            current.text += " " + line.strip()
        else:
            current = None
    return items


def _normalize(text: str) -> str:
    """Collapse whitespace and drop a leading checkbox, so pasted items match."""
    text = re.sub(r"^\s*- \[[ Xx-]\]\s*", "", text)
    return " ".join(text.split())


def _section(lines: list[str], line: int) -> tuple[int, int]:
    """
    Return the heading line above `line`, and where that heading's body ends.

    The body ends at the next heading of any level, as org's checkbox statistics do.
    """
    start = line
    while start >= 0 and not HEADING_RE.match(lines[start]):
        start -= 1
    if start < 0:
        raise ValueError("the item is not under a heading")
    end = line + 1
    while end < len(lines) and not HEADING_RE.match(lines[end]):
        end += 1
    return start, end


def _recount(heading: str, items: list[Item]) -> str:
    """
    Rewrite the progress cookies in a heading from the items under it.

    Org counts only the top-level items of a section by default, so nested items are
    left out of the count.
    """
    done = sum(1 for i in items if i.done)
    total = len(items)
    heading = FRACTION_COOKIE_RE.sub(f"[{done}/{total}]", heading)
    return PERCENT_COOKIE_RE.sub(f"[{100 * done // max(total, 1)}%]", heading)


###############################################################################
#
def task_entry(get_task_text: str) -> str:
    """
    Return the org entry from emacs-org-mcp's `get_task` result.

    `get_task` prints a status line, a `Section:` line and a blank line before the
    entry. What follows is exactly what `update_task` takes back as `task_entry`.
    """
    lines = get_task_text.split("\n")
    if len(lines) < 3 or not lines[1].startswith("Section:"):
        raise ValueError("not a get_task result")
    return "\n".join(lines[3:])


def tick_item(entry: str, item_text: str) -> tuple[str, str]:
    """
    Check off one item in an org task entry, and update its section's cookie.

    Args:
        entry: the task entry, as `get_task` returns it and `update_task` takes it
        item_text: all or part of the item's text; a leading `- [ ]` is ignored

    Returns:
        The new entry, and the section heading as it now reads.

    Raises:
        ValueError: if no item matches, more than one does, the item is already
            checked, or the item is nested or has nested items of its own. Those
            cases need a person or a direct edit.
    """
    lines = entry.split("\n")
    wanted = _normalize(item_text)
    if not wanted:
        raise ValueError("no item text given")
    items = _items(lines)
    matches = [i for i in items if wanted in _normalize(i.text)]
    if not matches:
        raise ValueError(f"no checklist item matches {wanted!r}")
    if len(matches) > 1:
        found = "; ".join(m.text[:60] for m in matches)
        raise ValueError(f"{len(matches)} items match {wanted!r}: {found}")
    item = matches[0]
    if item.done:
        raise ValueError(f"already checked: {item.text[:80]}")
    if item.box == "-":
        raise ValueError(f"partly done, so it has nested items: {item.text[:80]}")

    start, end = _section(lines, item.line)
    in_section = [i for i in items if start < i.line < end]
    top = min(i.indent for i in in_section)
    if item.indent > top:
        raise ValueError("nested items aren't handled yet; edit the task directly")
    following = [i for i in in_section if i.line > item.line]
    if following and following[0].indent > item.indent:
        raise ValueError("items with nested items aren't handled yet")

    lines[item.line] = lines[item.line].replace("- [ ]", "- [X]", 1)
    item.box = "X"
    lines[start] = _recount(lines[start], [i for i in in_section if i.indent == top])
    return "\n".join(lines), lines[start]


async def complete_task_item(
    call: Call, identifier: str, item_text: str, dry_run: bool = False
) -> str:
    """
    Check off one checklist item in a task, through emacs-org-mcp.

    Args:
        call: calls one of emacs-org-mcp's tools, and returns the tool's text
        identifier: the task's CUSTOM_ID, ticket ID or headline, as `get_task` takes
        item_text: all or part of the item's text
        dry_run: show the change without making it

    Returns:
        One line saying what changed, or, for a dry run, a unified diff of the entry.
        emacs-org-mcp sets the task's `:MODIFIED:` stamp itself.

    Raises:
        ValueError: if `tick_item` refuses the item. Nothing is changed.
    """
    entry = task_entry(await call("get_task", {"identifier": identifier}))
    new_entry, heading = tick_item(entry, item_text)
    if dry_run:
        return "\n".join(
            difflib.unified_diff(
                entry.split("\n"), new_entry.split("\n"), lineterm="", n=0
            )
        )
    await call("update_task", {"identifier": identifier, "task_entry": new_entry})
    return f"checked off 1 item in {identifier}; {heading.lstrip('* ')}"
