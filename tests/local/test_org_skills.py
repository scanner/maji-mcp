"""
Tests for how `org_skills.py` changes an org task's entry.

These need no emacs-org-mcp: they work on an entry's text. The same skill is run
against a real emacs-org-mcp in `test_emacs_org_bridge.py`.

    make test-local
"""

# 3rd party imports
import pytest
from pytest_check import check

# project imports
from tests.local.org_skills import tick_item

pytestmark = pytest.mark.local

# One task with a checked item, an open item, and an open item that has nested items,
# under a heading that counts the top-level items.
#
ENTRY = """\
** TODO Plan the release
*** Description
A made-up task.
*** Task items [1/3]
- [X] write the notes
- [ ] tag the commit
- [ ] publish the images
  - [ ] the service image
  - [ ] the runtime image
*** Notes
- [ ] an item under another heading"""


def test_one_item_is_checked_and_its_section_recounted() -> None:
    """
    GIVEN: A task entry whose checklist counts 1 of 3 top-level items as done
     WHEN: tick_item is given part of an open item's text
     THEN: that item is checked, and its section's heading counts 2 of 3
      AND: every other line of the entry is as it was, including the items under
           another heading
    """
    new_entry, heading = tick_item(ENTRY, "tag the")

    expected = ENTRY.replace("- [ ] tag the commit", "- [X] tag the commit").replace(
        "*** Task items [1/3]", "*** Task items [2/3]"
    )
    with check:
        assert new_entry == expected
    with check:
        assert heading == "*** Task items [2/3]"


@pytest.mark.parametrize(
    ("item_text", "reason"),
    [
        pytest.param("ship it", "no checklist item matches", id="no item matches"),
        pytest.param("the", "6 items match", id="several items match"),
        pytest.param("write the notes", "already checked", id="already checked"),
        pytest.param(
            "the service image", "nested items aren't handled", id="a nested item"
        ),
        pytest.param(
            "publish the images",
            "items with nested items aren't handled",
            id="an item with nested items",
        ),
        pytest.param("- [ ]", "no item text given", id="no text"),
    ],
)
def test_an_item_that_cannot_be_changed_exactly_is_refused(
    item_text: str, reason: str
) -> None:
    """
    GIVEN: A task entry with a checked item, open items, and an item that has nested
           items
     WHEN: tick_item is given text that matches no item, or several, or an item that
           is already checked, is nested, or has nested items of its own
     THEN: it raises ValueError saying which, so that nothing is written
    """
    with pytest.raises(ValueError, match=reason):
        tick_item(ENTRY, item_text)
