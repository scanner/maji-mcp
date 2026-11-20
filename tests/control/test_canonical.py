"""
Tests for canonical JSON and the hashes decisions are bound to (`canonical.py`).
"""

# system imports
import base64
import json
import re
from typing import Any

# 3rd party imports
import pytest

# project imports
from maji_mcp.control.canonical import (
    CONTENT_HASH_EXCLUDES,
    NotCanonical,
    canonical,
    challenge,
    content_hash,
    decision_hash,
)

# RFC 8785's own test data (section 3.2.2's string, and section 3.2.3's keys), as JSON
# text. Each escape is written `{hhhh}` and turned back into the RFC's own escape by
# `_u`, so tools that decode escapes while editing can't change the data. `_text`
# gives what such JSON text holds, for the characters the RFC's output has as they are.
#


def _u(text: str) -> str:
    """JSON text with each `{hhhh}` written as the escape it stands for."""
    return re.sub(r"\{([0-9a-fA-F]{4})\}", lambda m: "\\u" + m.group(1), text)


def _text(escaped: str) -> str:
    """The characters that `{hhhh}` escapes stand for."""
    held: str = json.loads(f'"{_u(escaped)}"')
    return held


RFC_STRING = _u(r'''"{20ac}${000F}{000a}A'{0042}{0022}{005c}\\\"\/"''')
RFC_STRING_CANONICAL = _u(r'''"€${000f}\nA'B\"\\\\\"/"''')
RFC_KEYS = _u(
    r"""{
  "{20ac}": "Euro Sign",
  "\r": "Carriage Return",
  "{fb33}": "Hebrew Letter Dalet With Dagesh",
  "1": "One",
  "{d83d}{de00}": "Emoji: Grinning Face",
  "{0080}": "Control",
  "{00f6}": "Latin Small Letter O With Diaeresis"
}"""
)

# The keys in the order the RFC gives for them: by UTF-16 code unit, which puts the
# emoji, a surrogate pair starting D83D, before the Hebrew letter at FB33, where code
# point order would put it after. Only the carriage return is escaped in the output.
#
RFC_KEYS_CANONICAL = (
    r'{"\r":"Carriage Return",'
    '"1":"One",'
    f'"{_text("{0080}")}":"Control",'
    f'"{_text("{00f6}")}":"Latin Small Letter O With Diaeresis",'
    f'"{_text("{20ac}")}":"Euro Sign",'
    f'"{_text("{d83d}{de00}")}":"Emoji: Grinning Face",'
    f'"{_text("{fb33}")}":"Hebrew Letter Dalet With Dagesh"'
    "}"
)


@pytest.mark.parametrize(
    ("sample", "expected"),
    [(RFC_STRING, RFC_STRING_CANONICAL), (RFC_KEYS, RFC_KEYS_CANONICAL)],
    ids=["a string to escape", "keys to order"],
)
def test_rfc_8785s_samples_come_out_as_it_gives_them(
    sample: str, expected: str
) -> None:
    """
    GIVEN: RFC 8785's sample string, with control characters, quotes and backslashes,
           and its sample object, whose keys include an emoji past U+FFFF and a
           Hebrew letter just below it
     WHEN: each is made canonical
     THEN: the string comes out exactly as the RFC's expected text
      AND: the object's keys come out in the RFC's order, which a browser's
           JavaScript also produces, rather than in code point order
    """
    assert canonical(json.loads(sample)) == expected


@pytest.mark.parametrize(
    "value",
    [1.5, 2**53, -(2**53), "\udead", {1: "a"}, {"a": {1, 2}}],
    ids=["float", "big", "big-negative", "lone-surrogate", "int-key", "set"],
)
def test_values_without_one_canonical_form_are_refused(value: Any) -> None:
    """
    GIVEN: A float, an integer past ±(2**53 − 1), a lone surrogate, a non-string key,
           or a type JSON doesn't have
     WHEN: it is made canonical
     THEN: it is refused, rather than hashed in a form another client might not
           reproduce
    """
    with pytest.raises(NotCanonical):
        canonical(value)


def test_the_contracts_worked_example_holds_for_the_implementation(
    spec: dict[str, Any],
) -> None:
    """
    GIVEN: The contract's worked example of one decision and a nonce
     WHEN: the control plane computes the decision hash and the challenge
     THEN: they match the example
      AND: a field the decision left out hashes the same as one sent as null, as
           the contract defines
    """
    example = spec["components"]["schemas"]["DecisionChoice"]["x-maji-example"]
    assert canonical(example["choice"]) == example["canonical"]
    hashed = decision_hash(example["choice"])
    assert hashed == example["decision_hash"]
    assert decision_hash({**example["choice"], "note": None}) == hashed
    encoded = base64.urlsafe_b64encode(challenge(example["nonce"], [hashed]))
    assert encoded.decode().rstrip("=") == example["challenge_base64url"]


def test_the_content_hash_leaves_out_what_the_contract_lists(
    spec: dict[str, Any],
) -> None:
    """
    GIVEN: The contract's list of the fields an item's content hash leaves out
     WHEN: the control plane's own list is compared with it
     THEN: they are the same, and changing a left-out field leaves the hash alone
           while changing a fact changes it
    """
    listed = spec["components"]["schemas"]["Item"]["x-maji-content-hash-excludes"]
    assert set(listed) == CONTENT_HASH_EXCLUDES
    item = {"kind": "changeset", "facts": [{"label": "Size", "value": "+1"}]}
    same = content_hash(item)
    assert content_hash({**item, "state": "decided", "notes": ["x"]}) == same
    changed = {**item, "facts": [{"label": "Size", "value": "+2"}]}
    assert content_hash(changed) != same
