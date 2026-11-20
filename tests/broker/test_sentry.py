"""
Tests for the `sentry` stubs, end to end through the broker, against a fake Sentry.

The fake answers the REST paths the stubs use, from fixture data, through httpx's
mock transport, so no request leaves the process.
"""

# system imports
import json
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any

# 3rd party imports
import httpx
import pytest
from pytest_check import check

# project imports
from maji_mcp.broker import sentry
from maji_mcp.broker.catalog import CallContext
from maji_mcp.common.protocol import MajiError
from tests.support.services import SENTRY_TOKEN, FakeSentry, Services, fixed_key


async def test_issues_use_the_org_endpoint_with_every_environment(
    sentry_services: tuple[Services, FakeSentry],
    run_script: Callable[..., Awaitable[Any]],
) -> None:
    """
    GIVEN: A script asking for a project's issues in two environments
     WHEN: it calls sentry.issues through the broker
     THEN: the broker queries the organization issues endpoint by the project's
           numeric id, sends both environments, follows Sentry's next-page link,
           and returns every issue from both pages
    """
    _, fake = sentry_services
    issues = await run_script(
        lambda maji: maji.call(
            "sentry.issues", project="api", environments=["prod-us1", "prod-eu1"]
        )
    )

    first = next(r for r in fake.requests if r.url.path.endswith("/issues/"))
    with check:
        assert len(issues) == 150
    with check:
        assert first.url.params.get_list("environment") == ["prod-us1", "prod-eu1"]
    with check:
        assert first.url.params["project"] == "42"
    with check:
        assert first.url.params["statsPeriod"] == "14d"


@pytest.mark.usefixtures("sentry_services")
async def test_a_script_counts_in_code_and_prints_little(
    run_script: Callable[..., Awaitable[Any]],
) -> None:
    """
    GIVEN: 150 issues across three culprits
     WHEN: a script groups them with the stub client's namespace, as an agent would
     THEN: the broker returns the issues to the script, and the script's answer is the
           three culprits with their totals
    """

    def script(maji: ModuleType) -> dict[str, int]:
        sentry_ns = maji.namespaces()["sentry"]
        totals: dict[str, int] = {}
        for issue in sentry_ns.issues(project="api"):
            totals[issue["culprit"]] = totals.get(issue["culprit"], 0) + int(
                issue["count"]
            )
        return totals

    totals = await run_script(script)
    assert sum(totals.values()) == sum(range(1, 151))
    assert sorted(totals) == ["web.tasks.job_0", "web.tasks.job_1", "web.tasks.job_2"]


@pytest.mark.usefixtures("sentry_services")
async def test_full_events_carry_their_top_in_app_frame(
    run_script: Callable[..., Awaitable[Any]],
) -> None:
    """
    GIVEN: An issue whose event's stack trace ends in library code
     WHEN: a script asks for its full events
     THEN: each event carries top_frame, the innermost frame in the app's own code
    """
    events = await run_script(
        lambda maji: maji.call("sentry.events", issue_id="7", full=True)
    )
    assert events[0]["top_frame"] == "web/tasks.py:88 ingest"


@pytest.mark.usefixtures("sentry_services")
@pytest.mark.parametrize(
    ("stub", "args", "code"),
    [
        ("sentry.environments", {"project": "missing"}, "not_found"),
        ("sentry.issues", {"project": "nope"}, "not_found"),
        ("sentry.tag_values", {"issue_id": "9", "key": "environment"}, "unavailable"),
    ],
    ids=[
        "Sentry answers 404",
        "a project the organization doesn't list",
        "Sentry answers 500",
    ],
)
async def test_sentry_failures_come_back_as_contract_codes(
    script_error: Callable[..., Awaitable[Any]],
    stub: str,
    args: dict[str, Any],
    code: str,
) -> None:
    """
    GIVEN: A call Sentry can't answer: for a project it has nothing at, for one the
           organization doesn't list, or with a server error
     WHEN: a script makes it through the broker
     THEN: the script gets a StubError with the contract's code, and the message
           never contains the broker's Sentry token
    """
    error = await script_error(lambda maji: maji.call(stub, **args))
    assert error.code == code
    assert SENTRY_TOKEN not in json.dumps([error.message, error.data])


@pytest.mark.usefixtures("sentry_services")
async def test_the_token_never_reaches_a_script(
    run_script: Callable[..., Awaitable[Any]],
) -> None:
    """
    GIVEN: The broker holds a Sentry token
     WHEN: a script lists its stubs and calls each read stub
     THEN: no result or description contains the token
    """

    def script(maji: ModuleType) -> str:
        seen = [maji.describe(), maji.call("sentry.projects")]
        seen.append(maji.call("sentry.issues", project="api", max_pages=1))
        return json.dumps(seen)

    assert SENTRY_TOKEN not in await run_script(script)


@pytest.mark.parametrize(
    ("answer", "refused"),
    [
        pytest.param(200, None, id="taken"),
        pytest.param(401, "Sentry refused the key", id="refused"),
        pytest.param(403, "Sentry refused the key", id="forbidden"),
        pytest.param(
            404,
            "the key can't read a Sentry organization 'acme'",
            id="another organization's",
        ),
        pytest.param(500, "Sentry answered 500", id="Sentry failing"),
    ],
)
async def test_a_key_is_checked_by_reading_its_organization(
    answer: int, refused: str | None
) -> None:
    """
    GIVEN: A key pasted for Sentry, with an organization's slug
     WHEN: the broker checks the key before keeping it
     THEN: it reads that organization at the configured host, with the key as a
           bearer token, and Sentry taking it is the only way the key is kept
      AND: a key Sentry won't take is refused in words of the broker's own, which
           never quote Sentry's answer or the key
    """
    seen: list[httpx.Request] = []

    def answering(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(answer, json={"detail": f"echo {SENTRY_TOKEN}"})

    definition = sentry.service("https://sentry.test", httpx.MockTransport(answering))
    said = await definition.check(SENTRY_TOKEN, {"org": "acme"})

    with check:
        assert said == refused
    with check:
        assert [(r.url.host, r.url.path) for r in seen] == [
            ("sentry.test", "/api/0/organizations/acme/")
        ]
    with check:
        assert seen[0].headers["Authorization"] == f"Bearer {SENTRY_TOKEN}"


def test_sentry_asks_for_a_read_only_key_sent_only_to_its_host() -> None:
    """
    GIVEN: The Sentry host the broker is configured with
     WHEN: Sentry's definition as a service is built
     THEN: the key goes only to that host, as a bearer token, and needs only read
           permissions; it is made on that host's token page, and given with an
           organization slug
    """
    definition = sentry.service("https://sentry.example.com/").definition()

    assert definition == {
        "service": "sentry",
        "label": "Sentry",
        "method": "api_key",
        "host": "sentry.example.com",
        "sent_as": "Authorization: Bearer",
        "permissions": ["org:read", "project:read", "event:read"],
        "key_page": "https://sentry.example.com/settings/account/api/auth-tokens/",
        "settings": [
            {
                "name": "org",
                "label": "Organization slug",
                "pattern": "[a-z0-9][a-z0-9_-]{0,49}",
                "description": "As in your Sentry address: https://<slug>.sentry.io",
            }
        ],
    }


async def test_sentrys_rate_limit_reaches_the_script_as_rate_limited() -> None:
    """
    GIVEN: Sentry answering 429 with a Retry-After, as it may when a script makes
           many calls at once
     WHEN: a stub's request gets that answer
     THEN: it fails with rate_limited and the wait Sentry asked for, so the script
           can back off rather than treat Sentry as down
    """

    def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "7"})

    client = sentry.SentryClient(
        "https://sentry.test",
        fixed_key(SENTRY_TOKEN, org="acme"),
        transport=httpx.MockTransport(limited),
    )
    with pytest.raises(MajiError) as raised:
        await client.get(CallContext("reader", "run-1"), "organizations/acme/projects/")
    assert raised.value.code == "rate_limited"
    assert raised.value.data == {"retry_after_s": "7"}
