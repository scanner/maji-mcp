"""
Tests for the `github` stubs, end to end through the broker, against a pretend GitHub
(`src/maji_mcp/broker/github.py`).

The pretend GitHub answers through httpx's mock transport, so no request leaves the
process. Its answers follow the shapes GitHub's API gives, with made-up repositories.
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
from maji_mcp.broker import github
from tests.support.github import (
    FILES,
    GITHUB_API,
    GITHUB_TOKEN,
    MATCHES,
    PULLS,
    FakeGitHub,
)


async def test_pulls_reads_every_page_as_records(
    fake_github: FakeGitHub, run_script: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: A repository with 230 pull requests, which GitHub gives 100 to a page
     WHEN: a script asks for all of them, closed ones too
     THEN: the broker follows GitHub's next-page links and returns all 230, most
           recently updated first, each as a record: logins in place of GitHub's
           nested users
    """
    found = await run_script(
        lambda maji: maji.call("github.pulls", repo="acme/api", state="all")
    )
    first = fake_github.requests[0].url.params
    with check:
        assert len(found) == PULLS
    with check:
        assert (first["state"], first["sort"], first["per_page"]) == (
            "all",
            "updated",
            "100",
        )
    with check:
        assert found[0] == {
            "number": 230,
            "title": "ABC-230 change 230",
            "state": "closed",
            "draft": True,
            "author": "ann",
            "created": "2026-11-02T09:00:00Z",
            "updated": "2026-11-12T09:00:00Z",
            "closed": "2026-11-12T10:00:00Z",
            "merged": "2026-11-12T10:00:00Z",
            "base": "main",
            "head": "abc-230",
            "labels": ["backend"],
            "reviewers": ["cy"],
            "url": "https://github.test/acme/api/pull/230",
        }


async def test_pulls_stops_at_its_limit(
    fake_github: FakeGitHub, run_script: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: A repository with 230 pull requests
     WHEN: a script asks for at most 150
     THEN: it gets the 150 most recent, and the broker reads no page past them
    """
    found = await run_script(
        lambda maji: maji.call("github.pulls", repo="acme/api", limit=150)
    )
    assert (len(found), len(fake_github.requests)) == (150, 2)


async def test_pull_has_its_reviews_and_every_file(
    fake_github: FakeGitHub, run_script: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: A pull request reviewed twice, that changed 150 files over two pages
     WHEN: a script calls github.pull
     THEN: it gets the pull request's record with its body and sizes, both reviews in
           order, and all 150 files
    """
    found = await run_script(
        lambda maji: maji.call("github.pull", repo="acme/api", number=7)
    )
    with check:
        assert (found["number"], found["body"], found["changed_files"]) == (
            7,
            "Fixes ABC-7",
            FILES,
        )
    with check:
        assert [(r["author"], r["state"]) for r in found["reviews"]] == [
            ("cy", "CHANGES_REQUESTED"),
            ("cy", "APPROVED"),
        ]
    with check:
        assert len(found["files"]) == FILES
    with check:
        assert found["files"][0] == {
            "path": "src/f0.py",
            "status": "modified",
            "additions": 1,
            "deletions": 0,
        }


async def test_commits_narrow_by_time_path_and_ref(
    fake_github: FakeGitHub, run_script: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: A repository's commits
     WHEN: a script asks for those since a time, under a path, from a branch
     THEN: GitHub is asked for exactly those, the branch as GitHub's `sha`, and each
           commit comes back with its author's name, login, date and whole message
    """
    found = await run_script(
        lambda maji: maji.call(
            "github.commits",
            repo="acme/api",
            since="2026-11-01T00:00:00Z",
            path="src/app.py",
            ref="release",
        )
    )
    asked = dict(fake_github.requests[0].url.params)
    with check:
        assert asked == {
            "since": "2026-11-01T00:00:00Z",
            "path": "src/app.py",
            "sha": "release",
            "per_page": "100",
        }
    with check:
        assert found[0] == {
            "sha": f"{3:040x}",
            "author": "Ann Example",
            "login": "ann",
            "date": "2026-11-13T09:00:00Z",
            "message": "ABC-3 fix the thing\n\nLonger reason.",
            "url": f"https://github.test/acme/api/commit/{3:040x}",
        }


async def test_search_says_how_many_match_beside_what_it_read(
    fake_github: FakeGitHub, run_script: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: A search that matches 1,500 issues and pull requests
     WHEN: a script reads up to 250 of them
     THEN: it gets 250, with GitHub's total of matches beside them, and each says
           its repository and whether it is a pull request
    """
    found = await run_script(
        lambda maji: maji.call("github.search", query="org:acme ABC", limit=250)
    )
    with check:
        assert (found["total"], found["incomplete"], len(found["items"])) == (
            MATCHES,
            False,
            250,
        )
    with check:
        assert [(i["repo"], i["number"], i["pull"]) for i in found["items"][:2]] == [
            ("acme/api", 1, False),
            ("acme/api", 2, True),
        ]


async def test_repos_lists_what_the_key_can_read(
    fake_github: FakeGitHub, run_script: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: A key that can read two repositories
     WHEN: a script lists them
     THEN: it gets both, as `owner/name`, with their default branches
    """
    found = await run_script(lambda maji: maji.call("github.repos"))
    assert [(r["repo"], r["default_branch"]) for r in found] == [
        ("acme/api", "main"),
        ("acme/web", "main"),
    ]


@pytest.mark.parametrize(
    ("stub", "args", "code", "said"),
    [
        ("github.pulls", {"repo": "acme/old"}, "not_found", "moved"),
        ("github.pulls", {"repo": "acme/limited"}, "rate_limited", "rate limit"),
        ("github.pulls", {"repo": "acme/locked"}, "unavailable", "not accessible"),
        ("github.pulls", {"repo": "acme/missing"}, "not_found", "can't see it"),
        ("github.pulls", {"repo": "acme/broken"}, "unavailable", "GitHub 500"),
        ("github.search", {"query": "bad:1"}, "invalid_argument", "cannot be"),
        ("github.pulls", {"repo": "acme/.."}, "invalid_argument", "isn't a repo"),
        ("github.pulls", {"repo": "acme/api/../../user"}, "invalid_argument", "isn't"),
        ("github.pulls", {"repo": "acme/api", "state": "x"}, "invalid_argument", "`open`"),
        ("github.pull", {"repo": "acme/api", "number": 0}, "invalid_argument", "number"),
    ],
    ids=[
        "a renamed repository",
        "GitHub's rate limit",
        "a key without the permission",
        "a repository GitHub hasn't got",
        "GitHub failing",
        "a search GitHub refuses",
        "a name of dots",
        "a name that would leave the repository's address",
        "a state GitHub doesn't have",
        "a number that isn't one",
    ],
)  # fmt: skip
async def test_github_failures_come_back_as_contract_codes(
    fake_github: FakeGitHub,
    script_error: Callable[..., Awaitable[Any]],
    stub: str,
    args: dict[str, Any],
    code: str,
    said: str,
) -> None:
    """
    GIVEN: A call GitHub refuses, moves or can't answer, or arguments that would
           change the request's address
     WHEN: a script makes it through the broker
     THEN: the script gets a StubError with the contract's code, saying why, with
           GitHub's own reason where it gave one
      AND: a redirect is reported, never followed, and arguments that aren't a
           repository, a state or a number are refused before any request to GitHub
      AND: the message never contains the key
    """
    error = await script_error(lambda maji: maji.call(stub, **args))
    with check:
        assert (error.code, said in error.message) == (code, True)
    with check:
        assert GITHUB_TOKEN not in json.dumps([error.message, error.data])
    with check:
        assert all(r.url.host == "api.github.test" for r in fake_github.requests)
    if code == "invalid_argument" and "query" not in args:
        with check:
            assert fake_github.requests == []


async def test_a_next_page_on_another_host_is_not_followed(
    fake_github: FakeGitHub, run_script: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: A page of pull requests whose next-page link leads to another host
     WHEN: a script lists the pull requests
     THEN: it gets the page it read, and the broker never sends the key to the other
           host
    """
    found = await run_script(
        lambda maji: maji.call("github.pulls", repo="acme/elsewhere")
    )
    with check:
        assert len(found) == 1
    with check:
        assert {r.url.host for r in fake_github.requests} == {"api.github.test"}


async def test_the_key_goes_only_to_the_api_with_githubs_headers(
    fake_github: FakeGitHub, run_script: Callable[..., Awaitable[Any]]
) -> None:
    """
    GIVEN: The broker holding a GitHub key
     WHEN: a script makes several calls
     THEN: every request goes to the configured API, with the key as a bearer token
           and the API version the stubs were written against
      AND: nothing the script gets back holds the key
    """

    def script(maji: Any) -> str:
        return json.dumps(
            [
                maji.describe(),
                maji.call("github.repos"),
                maji.call("github.pull", repo="acme/api", number=7),
            ]
        )

    seen = await run_script(script)
    with check:
        assert {
            (
                r.url.host,
                r.headers["Authorization"],
                r.headers["X-GitHub-Api-Version"],
            )
            for r in fake_github.requests
        } == {("api.github.test", f"Bearer {GITHUB_TOKEN}", "2022-11-28")}
    with check:
        assert GITHUB_TOKEN not in seen


@pytest.mark.parametrize(
    ("answer", "refused"),
    [
        pytest.param(200, None, id="taken"),
        pytest.param(
            401,
            "GitHub refused the key, which may have expired or been revoked",
            id="refused",
        ),
        pytest.param(500, "GitHub answered 500", id="GitHub failing"),
    ],
)
async def test_a_key_is_checked_by_reading_its_user(
    answer: int, refused: str | None
) -> None:
    """
    GIVEN: A key pasted for GitHub
     WHEN: the broker checks the key before keeping it
     THEN: it reads the key's own user at the configured API, with the key as a
           bearer token; GitHub taking the key is the only way it is kept
      AND: a key GitHub won't take is refused in words of the broker's own
    """
    seen: list[httpx.Request] = []

    def answering(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(answer, json={"message": f"echo {GITHUB_TOKEN}"})

    definition = github.service(GITHUB_API, httpx.MockTransport(answering))
    said = await definition.check(GITHUB_TOKEN, {})
    with check:
        assert said == refused
    with check:
        assert [(r.url.host, r.url.path) for r in seen] == [
            ("api.github.test", "/user")
        ]
    with check:
        assert seen[0].headers["Authorization"] == f"Bearer {GITHUB_TOKEN}"


@pytest.mark.parametrize(
    ("url", "host", "key_page"),
    [
        (
            "https://api.github.com",
            "api.github.com",
            "https://github.com/settings/personal-access-tokens/new",
        ),
        (
            "https://git.example.com/api/v3",
            "git.example.com",
            "https://git.example.com/settings/personal-access-tokens/new",
        ),
    ],
    ids=["github.com", "GitHub Enterprise Server"],
)
def test_github_asks_for_a_read_only_key_sent_only_to_its_api(
    url: str, host: str, key_page: str
) -> None:
    """
    GIVEN: The GitHub API the broker is configured with: GitHub's own, or a GitHub
           Enterprise Server's
     WHEN: GitHub's definition as a service is built
     THEN: the key goes only to that API's host, as a bearer token, needs only read
           permissions, and is made on that GitHub's token page, with nothing given
           beside it
    """
    definition = github.service(url).definition()
    assert definition == {
        "service": "github",
        "label": "GitHub",
        "method": "api_key",
        "host": host,
        "sent_as": "Authorization: Bearer",
        "permissions": [
            "Metadata: read",
            "Contents: read",
            "Pull requests: read",
            "Issues: read",
        ],
        "key_page": key_page,
        "settings": [],
    }
