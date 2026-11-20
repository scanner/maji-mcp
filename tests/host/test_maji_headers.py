"""
Tests for `bin/maji-headers`, the helper Claude Code runs on the host as it connects,
which prints the headers it sends the gateway.
"""

# system imports
import json
import subprocess
from pathlib import Path

# 3rd party imports
from pytest_check import check

# project imports
from tests.support.paths import HEADERS_HELPER


def test_the_headers_helper_names_a_new_session_each_time_it_runs(
    tmp_path: Path,
) -> None:
    """
    GIVEN: The helper Claude Code runs as it connects (bin/maji-headers), and a token
           file
     WHEN: it runs twice
     THEN: each time it prints the token as a bearer header and a new session id of
           32 hex digits, as JSON
      AND: a file holding anything but a token's characters is refused, so nothing
           can break out of the JSON
    """
    # A token of each kind of character a token is made of is printed as it is, with
    # a session id that is new each time.
    #
    token = tmp_path / "claude-code.token"
    token.write_text("abc_DEF-123")
    runs = [
        json.loads(
            subprocess.run(
                [HEADERS_HELPER, token], capture_output=True, check=True
            ).stdout
        )
        for _ in range(2)
    ]
    ids = [r["X-Maji-Session"] for r in runs]
    with check:
        assert [r["Authorization"] for r in runs] == ["Bearer abc_DEF-123"] * 2
    with check:
        assert all(len(i) == 32 and set(i) <= set("0123456789abcdef") for i in ids)
    with check:
        assert ids[0] != ids[1]

    # A file whose content would close the JSON string and add a header of its own
    # is refused, and nothing is printed.
    #
    token.write_text('x", "X-Maji-Session": "fixed')
    refused = subprocess.run([HEADERS_HELPER, token], capture_output=True, text=True)
    with check:
        assert refused.returncode == 1
    with check:
        assert refused.stdout == ""
    with check:
        assert "doesn't hold a token" in refused.stderr
