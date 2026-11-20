"""
Tests for the checks the broker makes on what a run's registration says about its
mounts (`src/maji_mcp/broker/mounts.py`): the app the agent's client says it is, what
the runner found a mount would leave out, and the paths that need a grant of their
own.

The flow these checks are part of, from a script asking for a path to a grant in your
queue, is tested in `tests/runner/test_server.py`.
"""

# system imports
from collections.abc import Callable
from typing import Any

# 3rd party imports
import pytest

# project imports
from maji_mcp.broker.mounts import checked_client, checked_exact, checked_findings
from maji_mcp.common.protocol import MajiError

# The one path the run in these tests asks for, and what the runner found a mount of
# it would leave out, in the runner's shape.
#
ASKED = ["/h/api"]
FOUND = {"left_out": [".env"], "count": 1, "itself": False, "looked_at": 3}


@pytest.mark.parametrize(
    ("checked", "given", "kept"),
    [
        pytest.param(
            checked_client,
            ({"name": "claude-code"},),
            {"name": "claude-code"},
            id="an app with a name",
        ),
        pytest.param(
            checked_client,
            ({"name": "claude-code", "version": "2.1.322"},),
            {"name": "claude-code", "version": "2.1.322"},
            id="an app with a name and a version",
        ),
        pytest.param(checked_client, (None,), None, id="no app named"),
        pytest.param(
            checked_findings,
            ({"/h/api": FOUND}, ASKED),
            {"/h/api": FOUND},
            id="findings on a path asked for",
        ),
        pytest.param(
            checked_exact,
            (["/h/api/.env"], ["/h/api", "/h/api/.env"]),
            ["/h/api/.env"],
            id="a path asked for that needs its own grant",
        ),
        pytest.param(checked_exact, (None, ASKED), [], id="no path needs its own"),
    ],
)
def test_what_a_registration_says_in_the_right_shape_is_kept(
    checked: Callable[..., Any], given: tuple[Any, ...], kept: Any
) -> None:
    """
    GIVEN: What a run's registration says about its mounts, in the shape the broker
           takes: the agent's app as a name, with or without a version; findings by
           the paths the run asks for; and paths of the run's own that need a grant
           of their own
     WHEN: the broker checks each
     THEN: it is kept as given
      AND: an app that isn't named stays unnamed, and no list of paths needing their
           own grant is an empty list
    """
    assert checked(*given) == kept


@pytest.mark.parametrize(
    ("checked", "given"),
    [
        pytest.param(checked_client, ("claude-code",), id="an app as bare text"),
        pytest.param(checked_client, ({"version": "1"},), id="an app without a name"),
        pytest.param(
            checked_client, ({"name": "x" * 101},), id="an app with a long name"
        ),
        pytest.param(
            checked_client,
            ({"name": "x", "os": "y"},),
            id="an app with something besides a name and a version",
        ),
        pytest.param(
            checked_findings,
            ({"/h/other": FOUND}, ASKED),
            id="findings on a path the run didn't ask for",
        ),
        pytest.param(
            checked_findings,
            ({"/h/api": {**FOUND, "count": 0}}, ASKED),
            id="findings that count fewer than they name",
        ),
        pytest.param(
            checked_findings,
            ({"/h/api": {**FOUND, "itself": 1}}, ASKED),
            id="findings whose `itself` isn't true or false",
        ),
        pytest.param(
            checked_findings,
            ({"/h/api": {**FOUND, "left_out": [""]}}, ASKED),
            id="findings that name an empty name",
        ),
        pytest.param(
            checked_exact,
            (["/h/other"], ASKED),
            id="a path needing its own grant that the run didn't ask for",
        ),
        pytest.param(
            checked_exact, ("/h/api", ASKED), id="paths needing their own, as text"
        ),
    ],
)
def test_what_a_registration_says_in_any_other_shape_is_refused(
    checked: Callable[..., Any], given: tuple[Any, ...]
) -> None:
    """
    GIVEN: What a run's registration says about its mounts, in a shape the broker
           doesn't take: an app that isn't a name and perhaps a version, each short
           text; findings on a path the run didn't ask for, or not in the runner's
           shape; or a path needing its own grant that the run didn't ask for
     WHEN: the broker checks it
     THEN: it is refused as `invalid_argument`, so nothing the runner or the gateway
           passes on reaches your queue unchecked
    """
    with pytest.raises(MajiError) as refused:
        checked(*given)
    assert refused.value.code == "invalid_argument"
