"""
Print the tasks in one section of an org directory, as emacs-org-mcp's own parser
reads them.

`test_emacs_org_bridge.py` compares the broker's `org` record stubs with what this
file prints. The test runs the file inside emacs-org-mcp's environment, so the
parser's dependencies resolve:

    uv run --project <emacs-org-mcp> python tests/local/org_reference.py \\
        <emacs-org-mcp> <org directory> active

It only reads. No function that writes an org file is called.
"""

# system imports
import dataclasses
import json
import sys
from typing import Any


def tasks(checkout: str, org_dir: str, section: str) -> list[dict[str, Any]]:
    """
    Read one section's tasks with emacs-org-mcp's parser.

    Args:
        checkout: the emacs-org-mcp checkout whose parser is used
        org_dir: the org directory to read
        section: `active`, `completed`, or a section's own name

    Returns:
        One record for each task: the parser's fields without the task's body, and
        the counts of open and checked checklist items, nested items included.
    """
    sys.path.insert(0, checkout)
    import server
    from docopt import docopt
    from mcp_server.config import global_state, load_config
    from mcp_server.tasks import list_tasks

    # `server.__doc__` is the server's docopt usage text, so parsing it yields the
    # settings the MCP server starts with. docopt fills in `--org-dir`'s default, and
    # `load_config` ranks a flag above the `ORG_DIR` environment variable, so the
    # directory goes in as the flag.
    #
    config = load_config(docopt(server.__doc__, argv=[f"--org-dir={org_dir}"]))
    global_state.config = config
    shorthands = {
        "active": config.active_section,
        "completed": config.completed_section,
    }

    records = []
    for task in list_tasks(shorthands.get(section, section), None):
        record = dataclasses.asdict(task)
        lines = record.pop("content").splitlines()
        record["checklist_open"] = sum(
            1 for line in lines if line.lstrip().startswith("- [ ]")
        )
        record["checklist_done"] = sum(
            1 for line in lines if line.lstrip().startswith(("- [X]", "- [x]"))
        )
        records.append(record)
    return records


def main() -> None:
    checkout, org_dir, section = sys.argv[1:4]
    json.dump(tasks(checkout, org_dir, section), sys.stdout, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
