"""
Tests for the `jira` stubs, end to end through the broker, against a pretend Jira
(`src/maji_mcp/broker/jira.py`).

The pretend Jira answers through httpx's mock transport, so no request leaves the
process. Its answers follow the examples in Jira's own API description, with made-up
issues.
"""

# system imports
import json
from collections.abc import Awaitable, Callable
from typing import Any

# 3rd party imports
import httpx
import pytest
from pytest_check import check

# project imports
from maji_mcp.broker import jira
from tests.support.jira import (
    CLOUD,
    EMAIL,
    GATEWAY,
    ISSUES,
    JIRA_TOKEN,
    SITE,
    SITE_URL,
    FakeJira,
    doc,
)


async def test_issues_reads_every_page_of_a_search_as_records(
    fake_jira: FakeJira, run_script: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: A JQL query that matches 250 issues, which Jira gives 100 to a page
     WHEN: a script calls jira.issues with it
     THEN: the broker follows Jira's next-page tokens and returns all 250, each as a
           record: names in place of Jira's nested objects
      AND: it asks Jira for the fields the records are built from, and no others
    """
    found = await run_script(lambda maji: maji.call("jira.issues", jql="project = ABC"))
    searches = [r for r in fake_jira.requests if r.url.path.endswith("/search/jql")]
    with check:
        assert len(found) == ISSUES
    with check:
        assert len(searches) == 3
    with check:
        assert found[1] == {
            "key": "ABC-2",
            "id": "10002",
            "project": "ABC",
            "type": "Task",
            "summary": "Task 2",
            "status": "Done",
            "status_category": "done",
            "priority": "Medium",
            "assignee": None,
            "reporter": "Bo Example",
            "created": "2026-11-02T09:00:00.000+0000",
            "updated": "2026-11-12T09:00:00.000+0000",
            "resolved": "2026-11-12T09:00:00.000+0000",
            "resolution": "Done",
            "labels": ["backend"],
            "components": ["api"],
            "fix_versions": ["1.4"],
            "parent": "ABC-1",
        }
    with check:
        assert searches[0].url.params["fields"] == ",".join(jira.FIELDS)


async def test_issues_stops_at_its_limit(
    fake_jira: FakeJira, run_script: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: A query that matches 250 issues
     WHEN: a script asks for at most 150
     THEN: it gets the first 150, and the broker reads no page past them
    """
    found = await run_script(
        lambda maji: maji.call("jira.issues", jql="project = ABC", limit=150)
    )
    searches = [r for r in fake_jira.requests if r.url.path.endswith("/search/jql")]
    assert ([i["key"] for i in found[-1:]], len(searches)) == (["ABC-150"], 2)


async def test_issues_carries_other_fields_a_script_asks_for(
    fake_jira: FakeJira, run_script: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: Issues with custom fields: a number, a user, an option and rich text
     WHEN: a script asks jira.issues for those fields by id
     THEN: each record carries them under `fields`: the number as Jira gives it, the
           user as their name, the option as its value, and the rich text as text
    """
    wanted = [
        "customfield_10016",
        "customfield_10020",
        "customfield_10030",
        "customfield_10040",
    ]
    found = await run_script(
        lambda maji: maji.call(
            "jira.issues", jql="project = ABC", fields=wanted, limit=1
        )
    )
    assert found[0]["fields"] == {
        "customfield_10016": 1.0,
        "customfield_10020": "Cy Example",
        "customfield_10030": "High",
        "customfield_10040": "Steps\n\nto reproduce",
    }


async def test_issue_has_its_text_every_comment_its_links_and_subtasks(
    fake_jira: FakeJira, run_script: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: An issue with a rich-text description, three comments of which Jira gives
           the first two with the issue, a link each way, and a subtask
     WHEN: a script calls jira.issue
     THEN: the description is plain text, all three comments are there, read past
           the first two from the issue's comment pages, each link says how it
           relates from this issue's side, and the subtask is named
    """
    found = await run_script(lambda maji: maji.call("jira.issue", key="ABC-7"))
    with check:
        assert found["description"] == "Main order flow broken\n\nsince Tuesday"
    with check:
        assert [(c["author"], c["text"]) for c in found["comments"]] == [
            ("Ann Example", "Seen in prod"),
            ("Bo Example", "Fix up"),
            ("Ann Example", "Ok"),
        ]
    with check:
        assert found["links"] == [
            {
                "relation": "blocks",
                "key": "ABC-8",
                "summary": "Task 8",
                "status": "Done",
            },
            {
                "relation": "is blocked by",
                "key": "XYZ-2",
                "summary": "Upstream",
                "status": "To Do",
            },
        ]
    with check:
        assert found["subtasks"] == [
            {"key": "ABC-9", "summary": "Task 9", "status": "To Do"}
        ]
    with check:
        assert (found["key"], found["status"]) == ("ABC-7", "In Progress")


async def test_changelog_flattens_each_change_and_narrows_by_field(
    fake_jira: FakeJira, run_script: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: An issue whose history, over two pages, changed its status twice and its
           assignee once
     WHEN: a script asks for its changes to the status
     THEN: it gets the two status changes, oldest first, each with when, who, and
           the values before and after
    """
    found = await run_script(
        lambda maji: maji.call("jira.changelog", key="ABC-7", fields=["Status"])
    )
    assert found == [
        {
            "at": "2026-11-03T09:00:00.000+0000",
            "author": "Ann Example",
            "field": "status",
            "from": "To Do",
            "to": "In Progress",
        },
        {
            "at": "2026-11-12T09:00:00.000+0000",
            "author": "Bo Example",
            "field": "status",
            "from": "In Progress",
            "to": "Done",
        },
    ]


async def test_count_projects_and_fields(
    fake_jira: FakeJira, run_script: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: A site with three projects, over two pages, and a custom field
     WHEN: a script counts a query's issues, and lists the projects and the fields
     THEN: the count is Jira's, the projects are all three, and each field says
           whether it is custom
    """

    def script(maji: Any) -> Any:
        return (
            maji.call("jira.count", jql="project = ABC"),
            [p["key"] for p in maji.call("jira.projects")],
            maji.call("jira.fields")[1],
        )

    count, projects, field = await run_script(script)
    with check:
        assert count == ISSUES
    with check:
        assert projects == ["ABC", "OPS", "XYZ"]
    with check:
        assert field == {
            "id": "customfield_10016",
            "name": "Story Points",
            "custom": True,
            "type": "number",
        }


@pytest.mark.parametrize(
    ("stub", "args", "code", "said"),
    [
        ("jira.issues", {"jql": "bad = 1"}, "invalid_argument", "'bad' does not"),
        ("jira.issue", {"key": "ABC-404"}, "not_found", "or the key can't see it"),
        ("jira.issue", {"key": "ABC-429"}, "rate_limited", "rate limit"),
        ("jira.issue", {"key": "ABC-500"}, "unavailable", "Jira 500"),
        ("jira.issue", {"key": "../../myself"}, "invalid_argument", "isn't an issue"),
        ("jira.changelog", {"key": "ABC-1?x=1"}, "invalid_argument", "isn't an issue"),
        (
            "jira.issues",
            {"jql": "project = ABC", "fields": ["summary,*all"]},
            "invalid_argument",
            "field ids",
        ),
    ],
    ids=[
        "a query Jira refuses",
        "an issue Jira hasn't got",
        "Jira's rate limit",
        "Jira failing",
        "a key that would leave the issue's address",
        "a key carrying a query",
        "a field list carrying more fields",
    ],
)
async def test_jira_failures_come_back_as_contract_codes(
    fake_jira: FakeJira,
    script_error: Callable[..., Awaitable[Any]],
    stub: str,
    args: dict[str, Any],
    code: str,
    said: str,
) -> None:
    """
    GIVEN: A call Jira refuses or can't answer, or with an issue key or field id that
           would change the request's address
     WHEN: a script makes it through the broker
     THEN: the script gets a StubError with the contract's code, saying why, with
           Jira's own reason for a query it refused
      AND: a key or field id that isn't one is refused before any request to Jira
      AND: the message never contains the key
    """
    error = await script_error(lambda maji: maji.call(stub, **args))
    with check:
        assert (error.code, said in error.message) == (code, True)
    with check:
        assert JIRA_TOKEN not in json.dumps([error.message, error.data])
    if code == "invalid_argument" and "jql" not in args:
        with check:
            assert [r.url.path for r in fake_jira.requests] == []


async def test_the_key_goes_only_to_the_gateway(
    fake_jira: FakeJira, run_script: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: The broker holding a Jira key, with its site and email
     WHEN: a script makes several calls
     THEN: the site's cloud id is read once, from the site's tenant_info, with no
           key; every other request goes to the gateway, under the cloud id, with
           the key and email as basic auth
      AND: nothing the script gets back holds the key
    """

    def script(maji: Any) -> str:
        return json.dumps(
            [
                maji.describe(),
                maji.call("jira.projects"),
                maji.call("jira.issue", key="ABC-7"),
            ]
        )

    seen = await run_script(script)
    hosts = [(r.url.host, r.url.path.split("/rest/")[0]) for r in fake_jira.requests]
    with check:
        assert hosts[0] == (f"{SITE}.jira.test", "/_edge/tenant_info")
    with check:
        assert set(hosts[1:]) == {("gateway.test", f"/ex/jira/{CLOUD}")}
    with check:
        assert JIRA_TOKEN not in seen


@pytest.mark.parametrize(
    ("site", "answer", "refused"),
    [
        pytest.param(SITE, 200, None, id="taken"),
        pytest.param(SITE, 401, jira.REFUSED, id="refused"),
        pytest.param(SITE, 403, jira.REFUSED, id="forbidden"),
        pytest.param(SITE, 500, "Jira answered 500", id="Jira failing"),
        pytest.param(
            "nosuch", 200, "there's no Jira site nosuch.atlassian.net", id="no site"
        ),
    ],
)
async def test_a_key_is_checked_by_reading_a_project(
    site: str, answer: int, refused: str | None
) -> None:
    """
    GIVEN: A key pasted for Jira, with a site's name and an email
     WHEN: the broker checks the key before keeping it
     THEN: it reads the site's cloud id without the key, then one project at the
           gateway with the key and email as basic auth; Jira taking the key is the
           only way it is kept
      AND: a key Jira won't take is refused in words of the broker's own, naming the
           usual reasons, and a site that doesn't exist is named as such
    """
    seen: list[httpx.Request] = []

    def answering(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/_edge/tenant_info":
            found = request.url.host == f"{SITE}.jira.test"
            return httpx.Response(200 if found else 404, json={"cloudId": CLOUD})
        return httpx.Response(answer, json={"message": f"echo {JIRA_TOKEN}"})

    definition = jira.service(GATEWAY, SITE_URL, httpx.MockTransport(answering))
    said = await definition.check(JIRA_TOKEN, {"site": site, "email": EMAIL})

    with check:
        assert said == refused
    with check:
        assert "Authorization" not in seen[0].headers
    if site == SITE:
        with check:
            assert (seen[1].url.host, seen[1].url.path) == (
                "gateway.test",
                f"/ex/jira/{CLOUD}/rest/api/3/project/search",
            )
        with check:
            assert seen[1].headers["Authorization"].startswith("Basic ")


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, json={"cloudId": "../../evil"}),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, text="<html>"),
        httpx.Response(302, headers={"Location": "https://elsewhere.test/"}),
    ],
    ids=["an id that isn't one", "a list", "not JSON", "a redirect"],
)
async def test_a_site_whose_tenant_info_is_odd_has_no_cloud_id(
    answer: httpx.Response,
) -> None:
    """
    GIVEN: A site whose tenant_info answers with something other than a cloud id
     WHEN: the broker reads it
     THEN: the site counts as missing, so nothing odd goes into the gateway's
           address and no redirect is followed
    """
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: answer)
    ) as http:
        assert await jira.cloud_id(http, SITE_URL, SITE) is None


def test_jira_asks_for_a_scoped_read_key_sent_only_to_the_gateway() -> None:
    """
    GIVEN: Atlassian's API gateway, which scoped API tokens are sent to
     WHEN: Jira's definition as a service is built
     THEN: the key goes only to the gateway, as basic auth with an email, needs only
           read:jira-work, is made at your Atlassian account, and is given with the
           site's name and the email
    """
    definition = jira.service().definition()
    with check:
        assert {k: definition[k] for k in ("host", "sent_as", "permissions")} == {
            "host": "api.atlassian.com",
            "sent_as": "Authorization: Basic, with your email",
            "permissions": ["read:jira-work"],
        }
    with check:
        assert definition["key_page"].startswith("https://id.atlassian.com/")
    with check:
        assert [s["name"] for s in definition["settings"]] == ["site", "email"]


def test_adf_text_keeps_the_words_and_the_shape_of_a_document() -> None:
    """
    GIVEN: A rich-text document with a heading, a paragraph with a mention, a line
           break and a link card, a bulleted list with a nested list, a numbered
           list, a code block, a table and a rule
     WHEN: it is rendered as text
     THEN: the words are all there, blocks are separated by blank lines, list items
           are marked and nested ones indented, table cells are joined by `|`, and
           the link card is its address
    """

    def text(t: str) -> dict[str, Any]:
        return {"type": "text", "text": t}

    def para(*content: dict[str, Any]) -> dict[str, Any]:
        return {"type": "paragraph", "content": list(content)}

    def items(*content: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"type": "listItem", "content": c} for c in content]

    document = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 2}, "content": [text("Plan")]},
            para(
                text("Ask "),
                {"type": "mention", "attrs": {"text": "@Ann"}},
                {"type": "hardBreak"},
                {"type": "inlineCard", "attrs": {"url": "https://x.test/1"}},
            ),
            {
                "type": "bulletList",
                "content": items(
                    [
                        para(text("one")),
                        {
                            "type": "bulletList",
                            "content": items([para(text("one a"))]),
                        },
                    ],
                    [para(text("two"))],
                ),
            },
            {
                "type": "orderedList",
                "attrs": {"order": 3},
                "content": items([para(text("third"))], [para(text("fourth"))]),
            },
            {"type": "codeBlock", "content": [text("x = 1\ny = 2")]},
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [para(text("a"))]},
                            {"type": "tableCell", "content": [para(text("b"))]},
                        ],
                    }
                ],
            },
            {"type": "rule"},
            para(text("end")),
        ],
    }
    assert jira.adf_text(document) == (
        "Plan\n\n"
        "Ask @Ann\nhttps://x.test/1\n\n"
        "- one\n  - one a\n- two\n\n"
        "3. third\n4. fourth\n\n"
        "x = 1\ny = 2\n\n"
        "a | b\n\n"
        "---\n\n"
        "end"
    )


def test_adf_text_of_no_document_is_empty() -> None:
    """
    GIVEN: An issue with no description, which Jira gives as null
     WHEN: it is rendered as text
     THEN: the text is empty
    """
    assert (jira.adf_text(None), jira.adf_text(doc())) == ("", "")
