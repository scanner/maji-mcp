"""
Tests for the fuzzy search that finds grants from a fragment you remember.
"""

# 3rd party imports
import pytest
from pytest_check import check

# project imports
from maji_mcp.control.fuzzy import match


@pytest.mark.parametrize(
    ("query", "text", "ranges"),
    [
        ("srcapi", "reads of ~/src/api", [(11, 14), (15, 18)]),
        ("ipacrs", "reads of ~/src/api", None),
        ("org tick", "ticks in org:task-demo", [(0, 4), (9, 12)]),
        ("tick org", "ticks in org:task-demo", [(0, 4), (9, 12)]),
        ("org zzz", "ticks in org:task-demo", None),
        ("api", "~/src/API", [(6, 9)]),
        ("Api", "~/src/API", None),
        ("API", "~/src/API", [(6, 9)]),
        ("  ", "anything", []),
    ],
    ids=[
        "characters in order, with others between",
        "the same characters reversed",
        "two words",
        "the two words the other way round",
        "a word that doesn't appear",
        "lower case against capitals",
        "a capital the text doesn't have there",
        "capitals as the text has them",
        "an empty query",
    ],
)
def test_a_query_matches_where_its_characters_appear_in_order(
    query: str, text: str, ranges: list[tuple[int, int]] | None
) -> None:
    """
    GIVEN: A query and a text
     WHEN: the query is matched
     THEN: it matches if its characters appear in the text in order, with others
           between or not, and the match says which characters it used; with its
           characters reversed it doesn't match
      AND: a query of several words matches the same characters whichever order
           its words are in, and not at all if one of them doesn't appear
      AND: If it is all lower case, case is ignored; if it has a capital, case must
           match, as in fzf's smart case
      AND: an empty query matches with no characters marked, so a search box left
           empty lists everything
    """
    found = match(query, text)
    assert (found.ranges if found else None) == ranges


def test_the_better_a_match_fits_the_higher_it_ranks() -> None:
    """
    GIVEN: A text holding the query as a word and one holding its characters
           scattered, and a query of two words given in either order
     WHEN: each is matched
     THEN: the word scores higher than the scattered characters, so it lists first
      AND: the two orders of the words score the same, being the same match
    """
    word = match("api", "mounts of ~/src/api")
    scattered = match("api", "a pair of maps in ink")
    forward = match("org tick", "ticks in org:task-demo")
    backward = match("tick org", "ticks in org:task-demo")

    assert word is not None and scattered is not None
    with check:
        assert word.score > scattered.score
    with check:
        assert forward is not None and forward == backward
