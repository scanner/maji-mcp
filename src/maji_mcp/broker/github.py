"""
The `github` stubs: GitHub's REST API, read-only.

The broker holds the key: a fine-grained personal access token that can read the
repositories' metadata, contents, pull requests and issues, which you paste into the
"Connect GitHub" item (`connections.py`). It is sent only to the configured API host,
`https://api.github.com` unless `GITHUB_API_URL` names a GitHub Enterprise Server's
`https://<host>/api/v3`, and never appears in a result, an error or the run log.

GitHub links each page of a list to the next. The broker follows a link only to the
configured host, and treats a redirect, such as GitHub gives for a renamed repository,
as an error naming where it leads, so the key never goes anywhere else.

The stubs return a record for each pull request, commit or issue: logins and names in
place of GitHub's nested objects.
"""

# system imports
import re
import time
from typing import Any

# 3rd party imports
import httpx

# project imports
from maji_mcp.broker.catalog import CallContext, Param, Stub
from maji_mcp.broker.connections import KeyFor, Service
from maji_mcp.common.protocol import MajiError

VERSION = "1.0"
API_URL = "https://api.github.com"
PAGE = 100
NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')

# What the key has to be allowed, as a fine-grained token's repository permissions
# name it.
#
PERMISSIONS = (
    "Metadata: read",
    "Contents: read",
    "Pull requests: read",
    "Issues: read",
)

# A repository, as `owner/name`. A name of dots alone is refused, since it would
# change the request's address.
#
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.+$)[A-Za-z0-9._-]{1,100}")

# The headers every request carries besides the key: GitHub's JSON, at the API version
# the stubs were written against.
#
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


###############################################################################
#
def key_page(url: str) -> str:
    """Where to make a fine-grained token, for the API at `url`."""
    site = "https://github.com" if url == API_URL else url.removesuffix("/api/v3")
    return f"{site}/settings/personal-access-tokens/new"


def service(
    url: str = API_URL, transport: httpx.AsyncBaseTransport | None = None
) -> Service:
    """
    GitHub, as a service the broker calls with a key you paste.

    Args:
        url: GitHub's API, from the broker's configuration: the only place the key
            is sent
        transport: replaces the network, in tests
    """
    base = url.rstrip("/")

    async def check(key: str, settings: dict[str, str]) -> str | None:
        """Read the key's own user: GitHub takes the key, or says why not."""
        async with httpx.AsyncClient(timeout=30, transport=transport) as http:
            try:
                response = await http.get(
                    f"{base}/user",
                    headers={**HEADERS, "Authorization": f"Bearer {key}"},
                )
            except httpx.HTTPError as exc:
                return f"GitHub couldn't be reached ({type(exc).__name__})"
        if response.status_code in (401, 403):
            return "GitHub refused the key, which may have expired or been revoked"
        if response.status_code != 200:
            return f"GitHub answered {response.status_code}"
        return None

    return Service(
        name="github",
        label="GitHub",
        host=httpx.URL(base).host,
        sent_as="Authorization: Bearer",
        permissions=PERMISSIONS,
        key_page=key_page(base),
        check=check,
        url=base,
    )


class GitHubClient:
    """
    A small async client for the GitHub endpoints the stubs use.

    Args:
        url: GitHub's API, the only place the key is sent
        key_for: gives the key for each call
        transport: replaces the network, in tests
    """

    def __init__(
        self,
        url: str,
        key_for: KeyFor,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key_for = key_for
        self._base = httpx.URL(url.rstrip("/") + "/")
        self._http = httpx.AsyncClient(
            base_url=self._base, timeout=30, transport=transport, headers=HEADERS
        )

    async def get(
        self, ctx: CallContext, path: str, params: dict[str, str] | None = None
    ) -> Any:
        body, _ = await self._get(ctx, path, params or {})
        return body

    async def paged(
        self,
        ctx: CallContext,
        path: str,
        params: dict[str, str],
        limit: int,
        items: str | None = None,
    ) -> tuple[list[Any], dict[str, Any]]:
        """
        Collect the pages of a list, up to `limit` entries.

        Args:
            items: the key a page holds its list under, for an endpoint such as
                search, whose pages are objects

        Returns:
            The entries, and the first page's other keys, such as a search's
            `total_count`
        """
        found: list[Any] = []
        first: dict[str, Any] = {}
        url: str | None = path
        query: dict[str, str] = {**params, "per_page": str(min(PAGE, limit))}
        while url is not None and len(found) < limit:
            page, url = await self._get(ctx, url, query)
            query = {}
            if items is not None:
                if not first:
                    first = {k: v for k, v in page.items() if k != items}
                page = page.get(items) or []
            found.extend(page)
            if not page:
                break
        return found[:limit], first

    async def _get(
        self, ctx: CallContext, url: str, params: dict[str, str]
    ) -> tuple[Any, str | None]:
        key, _ = await self._key_for(ctx)
        try:
            # httpx replaces a URL's query with `params` even when it is empty, which
            # would drop the page from GitHub's next-page link, so an empty query is
            # passed as None.
            #
            response = await self._http.get(
                url,
                params=params or None,
                headers={"Authorization": f"Bearer {key}"},
            )
        except httpx.HTTPError as exc:
            raise MajiError(
                "unavailable", f"GitHub unreachable: {type(exc).__name__}"
            ) from None
        path = response.request.url.path
        status = response.status_code
        if status == 429 or (
            status == 403
            and (
                response.headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in response.headers
            )
        ):
            raise MajiError(
                "rate_limited",
                "GitHub's rate limit; wait, or make fewer calls at once",
                {"retry_after_s": _retry_after(response)},
            )
        if 300 <= status < 400:
            raise MajiError(
                "not_found",
                f"GitHub moved {path}, as for a renamed repository; ask for it by "
                "its new name",
                {"location": response.headers.get("Location")},
            )
        if status == 401:
            raise MajiError(
                "unavailable",
                "GitHub refused the key the user connected; they can connect a new "
                "one from the web app",
            )
        if status == 403:
            raise MajiError(
                "unavailable",
                f"the GitHub key the user connected can't read {path} "
                f"({_message(response)}); they can connect one allowed more",
            )
        if status == 404:
            raise MajiError(
                "not_found", f"GitHub has nothing at {path}, or the key can't see it"
            )
        if status in (400, 422):
            raise MajiError(
                "invalid_argument", f"GitHub rejected {path}: {_message(response)}"
            )
        if status >= 400:
            raise MajiError("unavailable", f"GitHub {status} for {path}")
        return response.json(), self._next(response)

    def _next(self, response: httpx.Response) -> str | None:
        """The next page's link, when it leads to the configured host."""
        match = NEXT_LINK_RE.search(response.headers.get("Link", ""))
        if match is None:
            return None
        link = httpx.URL(match.group(1))
        same = (link.scheme, link.host, link.port) == (
            self._base.scheme,
            self._base.host,
            self._base.port,
        )
        return str(link) if same else None


def _retry_after(response: httpx.Response) -> str | None:
    """How long GitHub asked to wait, in seconds, from either of its headers."""
    if "retry-after" in response.headers:
        return response.headers["retry-after"]
    reset = response.headers.get("x-ratelimit-reset")
    if reset and reset.isdigit():
        return str(max(0, int(reset) - int(time.time())))
    return None


def _message(response: httpx.Response) -> str:
    """GitHub's own reason for an error, such as a search it couldn't parse."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    reasons = [str(body.get("message", ""))]
    reasons += [
        str(e.get("message", ""))
        for e in body.get("errors") or []
        if isinstance(e, dict)
    ]
    return "; ".join(r for r in reasons if r)[:500]


def _repo(value: Any) -> str:
    if not isinstance(value, str) or not REPO.fullmatch(value):
        raise MajiError(
            "invalid_argument", f"{value!r} isn't a repository, such as `owner/name`"
        )
    return value


def _number(value: Any) -> int:
    if not isinstance(value, int) or value < 1:
        raise MajiError("invalid_argument", "number is a pull request's number")
    return value


def _limit(value: Any, default: int) -> int:
    return max(1, value) if isinstance(value, int) else default


def _login(user: Any) -> str | None:
    return user.get("login") if isinstance(user, dict) else None


def _labels(item: dict[str, Any]) -> list[str]:
    return [label.get("name") for label in item.get("labels") or []]


def pull_record(pull: dict[str, Any]) -> dict[str, Any]:
    """A pull request as a script reads it."""
    return {
        "number": pull.get("number"),
        "title": pull.get("title"),
        "state": pull.get("state"),
        "draft": pull.get("draft"),
        "author": _login(pull.get("user")),
        "created": pull.get("created_at"),
        "updated": pull.get("updated_at"),
        "closed": pull.get("closed_at"),
        "merged": pull.get("merged_at"),
        "base": (pull.get("base") or {}).get("ref"),
        "head": (pull.get("head") or {}).get("ref"),
        "labels": _labels(pull),
        "reviewers": [_login(u) for u in pull.get("requested_reviewers") or []],
        "url": pull.get("html_url"),
    }


def commit_record(commit: dict[str, Any]) -> dict[str, Any]:
    """A commit as a script reads it: who wrote it, when, and its whole message."""
    detail = commit.get("commit") or {}
    author = detail.get("author") or {}
    return {
        "sha": commit.get("sha"),
        "author": author.get("name"),
        "login": _login(commit.get("author")),
        "date": author.get("date"),
        "message": detail.get("message"),
        "url": commit.get("html_url"),
    }


def found_record(item: dict[str, Any]) -> dict[str, Any]:
    """An issue or pull request a search found."""
    return {
        "repo": str(item.get("repository_url", "")).split("/repos/", 1)[-1],
        "number": item.get("number"),
        "title": item.get("title"),
        "state": item.get("state"),
        "pull": "pull_request" in item,
        "author": _login(item.get("user")),
        "created": item.get("created_at"),
        "updated": item.get("updated_at"),
        "closed": item.get("closed_at"),
        "labels": _labels(item),
        "url": item.get("html_url"),
    }


###############################################################################
#
def stubs(client: GitHubClient) -> list[Stub]:
    """Return the `github` stubs, bound to a client."""

    async def repos(args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        found, _ = await client.paged(
            ctx, "user/repos", {"sort": "pushed"}, _limit(args["limit"], 1000)
        )
        return [
            {
                "repo": r.get("full_name"),
                "private": r.get("private"),
                "archived": r.get("archived"),
                "default_branch": r.get("default_branch"),
                "pushed": r.get("pushed_at"),
            }
            for r in found
        ]

    async def pulls(args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        if args["state"] not in ("open", "closed", "all"):
            raise MajiError("invalid_argument", "state is `open`, `closed` or `all`")
        params = {"state": args["state"], "sort": "updated", "direction": "desc"}
        if args["base"]:
            params["base"] = args["base"]
        found, _ = await client.paged(
            ctx,
            f"repos/{_repo(args['repo'])}/pulls",
            params,
            _limit(args["limit"], 300),
        )
        return [pull_record(p) for p in found]

    async def pull(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        path = f"repos/{_repo(args['repo'])}/pulls/{_number(args['number'])}"
        detail = await client.get(ctx, path)
        reviews, _ = await client.paged(ctx, f"{path}/reviews", {}, 1000)
        files, _ = await client.paged(ctx, f"{path}/files", {}, 3000)
        return {
            **pull_record(detail),
            "body": detail.get("body"),
            "merged_by": _login(detail.get("merged_by")),
            "commits": detail.get("commits"),
            "additions": detail.get("additions"),
            "deletions": detail.get("deletions"),
            "changed_files": detail.get("changed_files"),
            "reviews": [
                {
                    "author": _login(r.get("user")),
                    "state": r.get("state"),
                    "submitted": r.get("submitted_at"),
                }
                for r in reviews
            ],
            "files": [
                {
                    "path": f.get("filename"),
                    "status": f.get("status"),
                    "additions": f.get("additions"),
                    "deletions": f.get("deletions"),
                }
                for f in files
            ],
        }

    async def commits(args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        params = {
            name: args[arg]
            for name, arg in (
                ("since", "since"),
                ("until", "until"),
                ("path", "path"),
                ("author", "author"),
                ("sha", "ref"),
            )
            if args[arg]
        }
        found, _ = await client.paged(
            ctx,
            f"repos/{_repo(args['repo'])}/commits",
            params,
            _limit(args["limit"], 500),
        )
        return [commit_record(c) for c in found]

    async def search(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        if not isinstance(args["query"], str) or not args["query"].strip():
            raise MajiError("invalid_argument", "query is a GitHub search")
        found, first = await client.paged(
            ctx,
            "search/issues",
            {"q": args["query"]},
            _limit(args["limit"], 300),
            "items",
        )
        return {
            "total": first.get("total_count"),
            "incomplete": bool(first.get("incomplete_results")),
            "items": [found_record(item) for item in found],
        }

    limit_note = (
        "Returns at most `limit`: as many as that means there may be more, so narrow "
        "the query or raise the limit."
    )
    return [
        Stub(
            "github.repos",
            "The repositories the key can read, most recently pushed first.",
            {"limit": Param("int", "stop after this many repositories", 1000)},
            "list[dict]: each with repo (`owner/name`), private, archived, "
            "default_branch and pushed",
            repos,
            notes=limit_note,
        ),
        Stub(
            "github.pulls",
            "A repository's pull requests, most recently updated first.",
            {
                "repo": Param("str", "the repository, as `owner/name`"),
                "state": Param("str", "`open`, `closed` or `all`", "open"),
                "base": Param("str", "only those into this branch", None),
                "limit": Param("int", "stop after this many", 300),
            },
            "list[dict]: each with number, title, state, draft, author, created, "
            "updated, closed, merged, base, head, labels, reviewers (those asked to "
            "review) and url. Times are ISO 8601, UTC.",
            pulls,
            notes=f"{limit_note} A pull request's reviews and files are in "
            "github.pull.",
        ),
        Stub(
            "github.pull",
            "One pull request, with its reviews and the files it changed.",
            {
                "repo": Param("str", "the repository, as `owner/name`"),
                "number": Param("int", "the pull request's number"),
            },
            "dict: the record github.pulls gives, with body, merged_by, commits, "
            "additions, deletions, changed_files, reviews (each with author, state "
            "and submitted) and files (each with path, status, additions and "
            "deletions)",
            pull,
            notes="GitHub lists at most 3,000 files of a pull request.",
        ),
        Stub(
            "github.commits",
            "A repository's commits, newest first.",
            {
                "repo": Param("str", "the repository, as `owner/name`"),
                "since": Param("str", "only after this time, ISO 8601", None),
                "until": Param("str", "only before this time, ISO 8601", None),
                "path": Param("str", "only those that changed this path", None),
                "author": Param("str", "only those by this login or email", None),
                "ref": Param("str", "the branch, tag or commit to start from", None),
                "limit": Param("int", "stop after this many", 500),
            },
            "list[dict]: each with sha, author (the name in the commit), login, "
            "date, message and url",
            commits,
            notes=limit_note,
        ),
        Stub(
            "github.search",
            "Issues and pull requests matching a GitHub search.",
            {
                "query": Param(
                    "str",
                    "GitHub search syntax, such as `repo:owner/name is:pr ABC-123` "
                    "or `org:owner is:pr is:merged merged:>=2026-11-01`",
                ),
                "limit": Param("int", "stop after this many", 300),
            },
            "dict: total (how many match), incomplete (GitHub stopped searching "
            "early), and items, each with repo, number, title, state, pull, author, "
            "created, updated, closed, labels and url",
            search,
            notes="GitHub returns at most 1,000 matches of a search, whatever the "
            "total, and allows about 30 searches a minute. Narrow a search with "
            "more terms or dates to read past 1,000.",
        ),
    ]
