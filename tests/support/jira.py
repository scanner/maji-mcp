"""
A pretend Jira Cloud: a site's public tenant_info, and the REST API behind Atlassian's
gateway, answered from made-up issues shaped like the examples in Jira's own API
description.
"""

# system imports
import base64
import json
from typing import Any

# 3rd party imports
import httpx

# The gateway and the site address the tests configure, the site's name and cloud id,
# and the key and email it takes. All made up.
#
GATEWAY = "https://gateway.test"
SITE_URL = "https://{site}.jira.test"
SITE = "acme"
CLOUD = "0f0e0d0c-0b0a-4908-8706-050403020100"
EMAIL = "ann@example.com"
JIRA_TOKEN = "ATATT-fixture-secret-token"
API = f"/ex/jira/{CLOUD}/rest/api/3/"

# How many issues the site has, and how many it gives on each page of a search,
# whatever page size is asked for, so a test sees the pages followed.
#
ISSUES = 250
PAGE = 100


def doc(*paragraphs: str) -> dict[str, Any]:
    """An Atlassian Document Format document of plain paragraphs."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}
            for text in paragraphs
        ],
    }


def person(name: str) -> dict[str, Any]:
    """A user as Jira embeds one in an issue."""
    return {
        "accountId": name.lower().replace(" ", "-"),
        "displayName": name,
        "active": True,
    }


def issue(n: int) -> dict[str, Any]:
    """Issue `ABC-<n>`, as a search gives it."""
    category = ("new", "indeterminate", "done")[n % 3]
    return {
        "id": str(10000 + n),
        "key": f"ABC-{n}",
        "fields": {
            "summary": f"Task {n}",
            "status": {
                "name": ("To Do", "In Progress", "Done")[n % 3],
                "statusCategory": {"key": category},
            },
            "issuetype": {"name": "Task"},
            "priority": {"name": "Medium"},
            "assignee": person("Ann Example") if n % 2 else None,
            "reporter": person("Bo Example"),
            "created": "2026-11-02T09:00:00.000+0000",
            "updated": "2026-11-12T09:00:00.000+0000",
            "resolutiondate": "2026-11-12T09:00:00.000+0000" if n % 3 == 2 else None,
            "resolution": {"name": "Done"} if n % 3 == 2 else None,
            "labels": ["backend"],
            "components": [{"name": "api"}],
            "fixVersions": [{"name": "1.4"}],
            "parent": {"key": "ABC-1"} if n > 1 else None,
            "project": {"key": "ABC", "name": "Alphabetical"},
            "customfield_10016": float(n % 5),
            "customfield_10020": person("Cy Example"),
            "customfield_10030": {"self": "x", "id": "1", "value": "High"},
            "customfield_10040": doc("Steps", "to reproduce"),
        },
    }


def full_issue() -> dict[str, Any]:
    """ABC-7 as the issue endpoint gives it: the first 2 of its 3 comments."""
    found = issue(7)
    found["fields"].update(
        {
            "description": doc("Main order flow broken", "since Tuesday"),
            "comment": {
                "comments": COMMENTS[:2],
                "maxResults": 2,
                "startAt": 0,
                "total": 3,
            },
            "issuelinks": [
                {
                    "type": {
                        "name": "Blocks",
                        "inward": "is blocked by",
                        "outward": "blocks",
                    },
                    "outwardIssue": {
                        "key": "ABC-8",
                        "fields": {"summary": "Task 8", "status": {"name": "Done"}},
                    },
                },
                {
                    "type": {
                        "name": "Blocks",
                        "inward": "is blocked by",
                        "outward": "blocks",
                    },
                    "inwardIssue": {
                        "key": "XYZ-2",
                        "fields": {"summary": "Upstream", "status": {"name": "To Do"}},
                    },
                },
            ],
            "subtasks": [
                {
                    "key": "ABC-9",
                    "fields": {"summary": "Task 9", "status": {"name": "To Do"}},
                }
            ],
        }
    )
    return found


COMMENTS: list[dict[str, Any]] = [
    {
        "author": person(name),
        "body": doc(text),
        "created": f"2026-11-1{n}T10:00:00.000+0000",
        "updated": f"2026-11-1{n}T10:00:00.000+0000",
    }
    for n, (name, text) in enumerate(
        [
            ("Ann Example", "Seen in prod"),
            ("Bo Example", "Fix up"),
            ("Ann Example", "Ok"),
        ]
    )
]

# ABC-7's history, three changes over two pages: two of the status, one of the
# assignee.
#
CHANGES: list[dict[str, Any]] = [
    {
        "created": "2026-11-03T09:00:00.000+0000",
        "author": person("Ann Example"),
        "items": [
            {"field": "status", "fromString": "To Do", "toString": "In Progress"},
            {"field": "assignee", "fromString": None, "toString": "Ann Example"},
        ],
    },
    {
        "created": "2026-11-12T09:00:00.000+0000",
        "author": person("Bo Example"),
        "items": [{"field": "status", "fromString": "In Progress", "toString": "Done"}],
    },
]


class FakeJira:
    """Answers the gateway and the site's tenant_info, and records each request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host.endswith(".jira.test"):
            assert "Authorization" not in request.headers
            if host == f"{SITE}.jira.test" and path == "/_edge/tenant_info":
                return httpx.Response(200, json={"cloudId": CLOUD})
            return httpx.Response(404, text="<html>Page Unavailable</html>")
        assert host == "gateway.test"
        sent = base64.b64encode(f"{EMAIL}:{JIRA_TOKEN}".encode()).decode()
        if request.headers.get("Authorization") != f"Basic {sent}":
            return httpx.Response(401, json={"message": "Unauthorized"})
        if not path.startswith(API):
            return httpx.Response(404)
        return self._answer(request, path.removeprefix(API))

    def _answer(self, request: httpx.Request, path: str) -> httpx.Response:
        params = request.url.params
        start = int(params.get("startAt", "0"))
        if path == "project/search":
            # Three projects, two to a page.
            #
            projects = [
                {"key": key, "name": key.title(), "id": str(n), "projectTypeKey": kind}
                for n, (key, kind) in enumerate(
                    [("ABC", "software"), ("OPS", "service_desk"), ("XYZ", "software")]
                )
            ]
            page = projects[start : start + 2]
            return httpx.Response(
                200,
                json={
                    "values": page,
                    "startAt": start,
                    "total": 3,
                    "isLast": start + 2 >= 3,
                },
            )
        if path == "field":
            return httpx.Response(
                200,
                json=[
                    {"id": "summary", "name": "Summary", "custom": False,
                     "schema": {"type": "string"}},
                    {"id": "customfield_10016", "name": "Story Points",
                     "custom": True, "schema": {"type": "number"}},
                ],
            )  # fmt: skip
        if path == "search/jql":
            if "bad" in params["jql"]:
                return httpx.Response(
                    400,
                    json={
                        "errorMessages": ["Field 'bad' does not exist."],
                        "errors": {},
                    },
                )
            # Pages of PAGE, linked by a token naming the next page's first issue.
            #
            first = int(params.get("nextPageToken", "1"))
            size = min(PAGE, int(params.get("maxResults", "50")))
            numbers = range(first, min(first + size, ISSUES + 1))
            body: dict[str, Any] = {"issues": [issue(n) for n in numbers]}
            if numbers.stop <= ISSUES:
                body["nextPageToken"] = str(numbers.stop)
            else:
                body["isLast"] = True
            return httpx.Response(200, json=body)
        if path == "search/approximate-count" and request.method == "POST":
            asked = json.loads(request.content)
            return httpx.Response(200, json={"count": ISSUES if asked["jql"] else 0})
        if path == "issue/ABC-7":
            return httpx.Response(200, json=full_issue())
        if path == "issue/ABC-7/comment":
            comments = COMMENTS[start : start + 2]
            return httpx.Response(
                200, json={"comments": comments, "startAt": start, "total": 3}
            )
        if path == "issue/ABC-7/changelog":
            changes = CHANGES[start : start + 1]
            return httpx.Response(
                200,
                json={
                    "values": changes,
                    "startAt": start,
                    "total": 2,
                    "isLast": start + 1 >= 2,
                },
            )
        if path == "issue/ABC-429":
            return httpx.Response(429, headers={"Retry-After": "3"})
        if path == "issue/ABC-500":
            return httpx.Response(500, text=f"echo {JIRA_TOKEN}")
        return httpx.Response(
            404, json={"errorMessages": ["Issue does not exist"], "errors": {}}
        )
