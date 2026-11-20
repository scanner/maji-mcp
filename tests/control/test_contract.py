"""
Tests for the control plane's REST contract, `api/openapi.yaml`.

The document is written before the code, so these check the contract itself: that it
is valid OpenAPI, that every route which widens access asks for proof that you were
there, and that its worked example of the decision hash is right.
"""

# system imports
import base64
import hashlib
import json
import re
from typing import Any

# 3rd party imports
import pytest
from openapi_spec_validator import validate
from pytest_check import check

METHODS = ("get", "put", "post", "delete", "patch")

# The routes reachable without a session or a token: the health check, the
# provider's redirect back after an OAuth sign-in, signing in itself, and the two
# passkey routes that take a setup code before any passkey exists.
#
OPEN_ROUTES = {
    "health",
    "oauthCallback",
    "sessionOptions",
    "signIn",
    "passkeyRegistrationOptions",
    "registerPasskey",
}


@pytest.fixture(scope="module")
def operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """
    Every operation the contract defines, as its path, method and definition. They
    are gathered once for the module, since the tests only read them.
    """
    return [
        (path, method, op)
        for path, item in spec["paths"].items()
        for method, op in item.items()
        if method in METHODS
    ]


def _canonical(value: Any) -> str:
    """
    RFC 8785 canonical JSON, for the worked example only.

    For strings, objects and arrays whose keys are all below U+FFFF, as the example's
    are, `json.dumps` with these arguments gives the canonical form. Keys past U+FFFF
    sort differently, which is why the control plane has its own implementation
    (`maji_mcp.control.canonical`), checked against the RFC's test data.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def test_the_contract_is_valid_openapi(spec: dict[str, Any]) -> None:
    """
    GIVEN: The control plane's REST contract
     WHEN: it is checked against the OpenAPI 3.1 schema
     THEN: it is valid, so clients and the server can be built and tested from it
    """
    validate(spec)


def test_every_route_that_widens_access_asks_for_proof(
    spec: dict[str, Any], operations: list[tuple[str, str, dict[str, Any]]]
) -> None:
    """
    GIVEN: Every route in the contract that changes state
     WHEN: its access is read
     THEN: it says whether it widens access, narrows it, proposes a queue item, or is
           plumbing; and each route that widens access names the proof it needs, so
           no decision can be made with a token alone
    """
    # Each route is judged on its own, so one that falls short doesn't hide another.
    #
    for path, method, op in operations:
        if method == "get":
            continue
        access = op.get("x-maji-access")
        with check:
            assert access in {"widens", "narrows", "proposes", "none"}, (path, method)
        if access == "widens":
            with check:
                assert op.get("x-maji-proof"), (path, method)

    decide = spec["paths"]["/decisions"]["post"]
    assert decide["x-maji-proof"] == "assertion or unlocked-tab"
    assert spec["paths"]["/unlocks"]["post"]["x-maji-proof"] == "assertion"


def test_only_the_named_routes_are_open_without_a_session_or_token(
    spec: dict[str, Any], operations: list[tuple[str, str, dict[str, Any]]]
) -> None:
    """
    GIVEN: The contract's default of a session or a token on every route
     WHEN: each route's own security is read
     THEN: only the health check, the OAuth callback, signing in and registering a
           passkey can be reached without one
    """
    assert spec["security"] == [{"session": []}, {"clientToken": []}]
    open_routes = {
        op["operationId"]
        for _, _, op in operations
        if "security" in op and (op["security"] == [] or {} in op["security"])
    }
    assert open_routes == OPEN_ROUTES


def test_the_worked_decision_hash_is_right(spec: dict[str, Any]) -> None:
    """
    GIVEN: The contract's worked example of one decision and a nonce
     WHEN: the decision hash and the challenge are computed as the contract defines
           them
     THEN: they match the example, so the definition can be implemented by any
           client or instance and checked against this one
    """
    example = spec["components"]["schemas"]["DecisionChoice"]["x-maji-example"]
    canonical = _canonical(example["choice"])
    assert canonical == example["canonical"]

    # The example remembers the approval, so the pattern and how long it lasts are
    # part of what is signed.
    #
    assert '"remember":[{"lasts":"session","pattern":"p1"}]' in canonical

    decision_hash = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    assert decision_hash == example["decision_hash"]

    challenge = hashlib.sha256(
        _canonical({"nonce": example["nonce"], "decisions": [decision_hash]}).encode()
    ).digest()
    encoded = base64.urlsafe_b64encode(challenge).decode().rstrip("=")
    assert encoded == example["challenge_base64url"]


@pytest.mark.parametrize("schema", ["DecisionChoice", "Decision"])
def test_a_decision_never_carries_a_key(spec: dict[str, Any], schema: str) -> None:
    """
    GIVEN: A decision as chosen, and as recorded
     WHEN: its fields are read
     THEN: a pasted key appears only as its fingerprint; the key itself travels only
           in the decide request's `secrets`, which are never recorded
    """
    fields = spec["components"]["schemas"][schema]["properties"]
    assert "credential_fingerprint" in fields
    assert not {"secret", "secrets", "token", "key"} & set(fields)
    decide = spec["paths"]["/decisions"]["post"]["requestBody"]["content"]
    secrets = decide["application/json"]["schema"]["properties"]["secrets"]
    assert secrets["additionalProperties"]["writeOnly"] is True


def test_the_content_hash_covers_what_you_decide_on(spec: dict[str, Any]) -> None:
    """
    GIVEN: An item's content hash, which a decision is bound to
     WHEN: the fields it leaves out are read
     THEN: it leaves out only the item's state and bookkeeping, never what you decide
           on: the facts, the diff, the parts, the resources, the patterns an approval
           can be remembered with, the claim and your words
    """
    item = spec["components"]["schemas"]["Item"]
    excluded = set(item["x-maji-content-hash-excludes"])
    assert excluded <= set(item["properties"])
    decided_on = {
        "kind",
        "title",
        "subject",
        "skill",
        "requester",
        "facts",
        "diff",
        "claim",
        "words",
        "parts",
        "resources",
        "patterns",
        "choices",
        "preconditions",
        "depends_on",
    }
    assert decided_on <= set(item["properties"])
    assert not excluded & decided_on


@pytest.mark.parametrize(
    ("name", "accepted"),
    [
        ("studio-mac", True),
        ("build01", True),
        ("studio-mac-2", True),
        ("Studio-Mac", False),
        ("studio-mac.example.net", False),
        ("-studio", False),
        ("studio-", False),
    ],
)
def test_an_instance_is_named_after_its_host(
    spec: dict[str, Any], name: str, accepted: bool
) -> None:
    """
    GIVEN: A name for an instance
     WHEN: it is checked against the contract
     THEN: If it is one lower-case DNS label, such as a host's name or the first
           label of its domain name, with `-2` for a second instance on that host,
           it is accepted
      AND: If it has capitals, dots, or a hyphen at either end, it is refused, so it
           can always stand at the front of an item id
    """
    schemas = spec["components"]["schemas"]
    pattern = schemas["InstanceIdentifier"]["pattern"]
    assert bool(re.fullmatch(pattern, name)) is accepted
    item = f"{name}-01jc6z8q3v7m2k4t9r5x1y0b8n"
    assert bool(re.fullmatch(schemas["ItemIdentifier"]["pattern"], item)) is accepted


def test_a_remembered_approval_never_covers_content_the_requester_wrote(
    spec: dict[str, Any],
) -> None:
    """
    GIVEN: The patterns an item offers for remembering its approval
     WHEN: their kinds of access are read
     THEN: they cover reads, mounts and structural changes only, so a write of
           content the requester wrote comes to the queue each time
    """
    item = spec["components"]["schemas"]["Item"]
    narrowed = item["properties"]["patterns"]["items"]["allOf"][1]
    assert narrowed["properties"]["access"]["enum"] == ["read", "mount", "structural"]
