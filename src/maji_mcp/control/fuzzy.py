"""
Fuzzy matching, in the manner of fzf: a query's characters must appear in a text in
order, not necessarily together, so a fragment you remember is enough to find a grant.

A query of several words matches when each word does, in any order, as in fzf. Among
the ways a word can match, the best scores highest: characters that follow one
another, characters at the start of a word, and few characters skipped between them.
The match also says which characters matched, so a client can highlight them.
"""

# system imports
from dataclasses import dataclass

BASE = 16
CONSECUTIVE = 6
WORD_START = 8
CAMEL = 7
GAP = 1
SEPARATORS = frozenset(" /._-:@~")


@dataclass(frozen=True)
class Match:
    score: int
    ranges: list[tuple[int, int]]


def match(query: str, text: str) -> Match | None:
    """
    Find the best way each of the query's words appears in the text.

    Matching ignores case unless a word has a capital letter, as fzf's smart case
    does.

    Returns:
        The total score and the matched characters as start and end offsets, or None
        when some word's characters don't all appear in order
    """
    total, positions = 0, set()
    for word in query.split():
        found = _match_word(word, text)
        if found is None:
            return None
        total += found[0]
        positions.update(found[1])
    return Match(total, _ranges(sorted(positions)))


def _match_word(query: str, text: str) -> tuple[int, list[int]] | None:
    if query == query.lower():
        haystack, needle = text.lower(), query
    else:
        haystack, needle = text, query
    n, m = len(haystack), len(needle)
    if m > n:
        return None

    # `score[i][j]` is the best score for the first i + 1 query characters with the
    # last of them at text position j; `came[i][j]` is where the one before it was,
    # for reading the match back.
    #
    lowest = -(10**9)
    score = [[lowest] * n for _ in range(m)]
    came = [[-1] * n for _ in range(m)]
    for i in range(m):
        gap_best, gap_at = lowest, -1
        for j in range(n):
            # The best earlier position for the previous character with at least one
            # text character skipped since, each skipped character costing GAP.
            #
            if i and j >= 2:
                gap_best -= GAP
                if score[i - 1][j - 2] - GAP > gap_best:
                    gap_best, gap_at = score[i - 1][j - 2] - GAP, j - 2
            if haystack[j] != needle[i]:
                continue
            bonus = BASE + _bonus(text, j)
            if i == 0:
                score[i][j] = bonus
                continue
            adjacent = score[i - 1][j - 1] + CONSECUTIVE if j else lowest
            if adjacent >= gap_best and adjacent > lowest // 2:
                score[i][j], came[i][j] = adjacent + bonus, j - 1
            elif gap_best > lowest // 2:
                score[i][j], came[i][j] = gap_best + bonus, gap_at
    last = max(range(n), key=lambda j: score[m - 1][j])
    if score[m - 1][last] <= lowest // 2:
        return None

    positions = []
    j = last
    for i in range(m - 1, -1, -1):
        positions.append(j)
        j = came[i][j]
    positions.reverse()
    return score[m - 1][last], positions


def _bonus(text: str, j: int) -> int:
    if j == 0 or text[j - 1] in SEPARATORS:
        return WORD_START
    if text[j - 1].islower() and text[j].isupper():
        return CAMEL
    return 0


def _ranges(positions: list[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for p in positions:
        if ranges and ranges[-1][1] == p:
            ranges[-1] = (ranges[-1][0], p + 1)
        else:
            ranges.append((p, p + 1))
    return ranges
