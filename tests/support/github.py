"""
A pretend GitHub REST API, answered from made-up repositories, pull requests, commits
and search results shaped like GitHub's own answers.
"""

# system imports
from typing import Any

# 3rd party imports
import httpx

# The API address the tests configure, and the key it takes. Both made up.
#
GITHUB_API = "https://api.github.test"
GITHUB_TOKEN = "github_pat_fixture-secret-token"

# How many pull requests acme/api has, files its pull request 7 changed, and issues
# and pull requests a search matches. GitHub gives at most 100 to a page.
#
PULLS = 230
FILES = 150
MATCHES = 1500


def user(login: str) -> dict[str, Any]:
    """A user as GitHub embeds one."""
    return {"login": login, "id": len(login), "type": "User"}


def pull(n: int) -> dict[str, Any]:
    """Pull request `n` of acme/api, as a list of pull requests gives it."""
    return {
        "number": n,
        "title": f"ABC-{n} change {n}",
        "state": "open" if n % 2 else "closed",
        "draft": n % 5 == 0,
        "user": user("ann" if n % 3 else "bo"),
        "created_at": "2026-11-02T09:00:00Z",
        "updated_at": "2026-11-12T09:00:00Z",
        "closed_at": None if n % 2 else "2026-11-12T10:00:00Z",
        "merged_at": None if n % 2 else "2026-11-12T10:00:00Z",
        "base": {"ref": "main"},
        "head": {"ref": f"abc-{n}"},
        "labels": [{"name": "backend"}],
        "requested_reviewers": [user("cy")],
        "html_url": f"https://github.test/acme/api/pull/{n}",
    }


def commit(n: int) -> dict[str, Any]:
    """Commit `n` of acme/api."""
    return {
        "sha": f"{n:040x}",
        "commit": {
            "author": {"name": "Ann Example", "date": f"2026-11-1{n}T09:00:00Z"},
            "message": f"ABC-{n} fix the thing\n\nLonger reason.",
        },
        "author": user("ann"),
        "html_url": f"https://github.test/acme/api/commit/{n:040x}",
    }


def found(n: int) -> dict[str, Any]:
    """Issue or pull request `n` of acme/api, as a search gives it."""
    item = {
        "number": n,
        "title": f"ABC-{n}",
        "state": "open",
        "user": user("ann"),
        "created_at": "2026-11-02T09:00:00Z",
        "updated_at": "2026-11-12T09:00:00Z",
        "closed_at": None,
        "labels": [],
        "repository_url": f"{GITHUB_API}/repos/acme/api",
        "html_url": f"https://github.test/acme/api/issues/{n}",
    }
    if n % 2 == 0:
        item["pull_request"] = {"url": "x"}
    return item


class FakeGitHub:
    """Answers the GitHub API from fixtures, and records each request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host != "api.github.test":
            return httpx.Response(200, json=[{"stolen": True}])
        if request.headers.get("Authorization") != f"Bearer {GITHUB_TOKEN}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        return self._answer(request, request.url.path.removeprefix("/"))

    def _page(
        self, request: httpx.Request, items: list[Any], wrap: str | None = None
    ) -> httpx.Response:
        """One page of `items`, linked to the next the way GitHub links pages."""
        params = request.url.params
        size = min(100, int(params.get("per_page", "30")))
        number = int(params.get("page", "1"))
        page = items[(number - 1) * size : number * size]
        headers = {}
        if number * size < len(items):
            following = request.url.copy_merge_params({"page": str(number + 1)})
            headers["Link"] = f'<{following}>; rel="next", <{following}>; rel="last"'
        body: Any = page
        if wrap is not None:
            body = {"total_count": MATCHES, "incomplete_results": False, wrap: page}
        return httpx.Response(200, json=body, headers=headers)

    def _answer(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "user":
            return httpx.Response(200, json=user("ann"))
        if path == "user/repos":
            repos = [
                {
                    "full_name": name,
                    "private": True,
                    "archived": False,
                    "default_branch": "main",
                    "pushed_at": "2026-11-12T09:00:00Z",
                }
                for name in ("acme/api", "acme/web")
            ]
            return self._page(request, repos)
        if path == "repos/acme/api/pulls":
            return self._page(request, [pull(n) for n in range(PULLS, 0, -1)])
        if path == "repos/acme/api/pulls/7":
            detail = {
                **pull(7),
                "body": "Fixes ABC-7",
                "merged_by": None,
                "commits": 3,
                "additions": 40,
                "deletions": 2,
                "changed_files": FILES,
            }
            return httpx.Response(200, json=detail)
        if path == "repos/acme/api/pulls/7/reviews":
            reviews = [
                {"user": user("cy"), "state": "CHANGES_REQUESTED",
                 "submitted_at": "2026-11-03T09:00:00Z"},
                {"user": user("cy"), "state": "APPROVED",
                 "submitted_at": "2026-11-04T09:00:00Z"},
            ]  # fmt: skip
            return self._page(request, reviews)
        if path == "repos/acme/api/pulls/7/files":
            files = [
                {"filename": f"src/f{n}.py", "status": "modified", "additions": 1,
                 "deletions": 0}
                for n in range(FILES)
            ]  # fmt: skip
            return self._page(request, files)
        if path == "repos/acme/api/commits":
            return self._page(request, [commit(n) for n in (3, 2, 1)])
        if path == "search/issues":
            if "bad" in request.url.params["q"]:
                return httpx.Response(
                    422,
                    json={
                        "message": "Validation Failed",
                        "errors": [{"message": "The listed users cannot be searched"}],
                    },
                )
            return self._page(request, [found(n) for n in range(1, 1001)], "items")
        if path == "repos/acme/old/pulls":
            return httpx.Response(
                301,
                headers={"Location": f"{GITHUB_API}/repositories/9/pulls"},
                json={"message": "Moved Permanently"},
            )
        if path == "repos/acme/elsewhere/pulls":
            return httpx.Response(
                200,
                json=[pull(1)],
                headers={"Link": '<https://elsewhere.test/steal?page=2>; rel="next"'},
            )
        if path == "repos/acme/limited/pulls":
            return httpx.Response(
                403,
                json={"message": "API rate limit exceeded"},
                headers={"x-ratelimit-remaining": "0", "retry-after": "60"},
            )
        if path == "repos/acme/locked/pulls":
            return httpx.Response(
                403,
                json={"message": "Resource not accessible by personal access token"},
            )
        if path == "repos/acme/broken/pulls":
            return httpx.Response(500, text=f"echo {GITHUB_TOKEN}")
        return httpx.Response(404, json={"message": "Not Found"})
