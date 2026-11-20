"""
Tests for session names: three words picked by a hash of a session's key, the same
wherever they are worked out, from lists that never change.
"""

# system imports
import hashlib
import itertools
import re

# 3rd party imports
import pytest
from pytest_check import check

# project imports
from maji_mcp.common.names import ADJECTIVES, NOUNS, VERBS, session_name


def _apart(a: str, b: str) -> int:
    """How many letters must be added, removed or changed to turn `a` into `b`."""
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


@pytest.mark.parametrize(
    ("key", "name"),
    [
        ("0" * 64, "gardening-stony-canyon"),
        ("f" * 64, "whistling-saffron-acorn"),
        ("s-91c2", "skipping-graceful-peony"),
        ("tester-session", "tinkering-frothy-glen"),
    ],
)
def test_a_session_is_named_the_same_every_time(key: str, name: str) -> None:
    """
    GIVEN: The key of a session
     WHEN: it is named
     THEN: it gets the name it has always had, so a session keeps its name across
           restarts, in logs already written and in every client
    """
    assert session_name(key) == name


def test_the_word_lists_never_change() -> None:
    """
    GIVEN: The three word lists names are picked from
     WHEN: their sizes and words are read
     THEN: they are the lists every name so far was picked from, in the same order:
           a word changed, moved or added would rename every session
    """
    assert (len(VERBS), len(ADJECTIVES), len(NOUNS)) == (133, 255, 288)
    joined = "\n".join((*VERBS, "", *ADJECTIVES, "", *NOUNS))
    assert (
        hashlib.sha256(joined.encode()).hexdigest()
        == "944518ed93db309ec738acbdffc706268377ae0b2ca4685490d77c4bc3317fab"
    )


@pytest.mark.parametrize(
    "words", [VERBS, ADJECTIVES, NOUNS], ids=["verbs", "adjectives", "nouns"]
)
def test_no_two_words_in_a_list_are_easily_mistaken(words: tuple[str, ...]) -> None:
    """
    GIVEN: One of the three word lists
     WHEN: every pair of its words is compared
     THEN: each word is lowercase letters alone, used once, and at least two letters
           from every other word in the list, so a name one letter off another isn't
           a name at all
      AND: every verb is a present participle
    """
    with check:
        assert len(set(words)) == len(words)
    with check:
        assert all(re.fullmatch(r"[a-z]{3,10}", w) for w in words)
    with check:
        near = [(a, b) for a, b in itertools.combinations(words, 2) if _apart(a, b) < 2]
        assert near == []
    if words is VERBS:
        with check:
            assert all(verb.endswith("ing") for verb in words)


def test_a_name_is_a_verb_an_adjective_and_a_noun() -> None:
    """
    GIVEN: Many different session keys
     WHEN: each is named
     THEN: each name is a present participle, an adjective and a noun from the lists,
           joined by hyphens
      AND: keys spread across every word of every list, so a few hundred sessions
           all have different names
    """
    keys = [hashlib.sha256(str(n).encode()).hexdigest() for n in range(5000)]
    picked = [session_name(key).split("-") for key in keys]
    verbs, adjectives, nouns = (set(column) for column in zip(*picked, strict=True))
    assert (verbs, adjectives, nouns) == (set(VERBS), set(ADJECTIVES), set(NOUNS))
    assert len({session_name(key) for key in keys[:300]}) == 300
