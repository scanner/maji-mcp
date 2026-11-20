"""
The `sentry` stubs: Sentry's REST API, read-only.

Sentry is reached through its REST API rather than its MCP server, because a script
needs every issue and its counts in the environments it asks about. The MCP server's
issue search returns 100 issues at most, with no way to the rest; counts every
environment whatever the search filters on; and runs a model on every search
(`docs/decisions.md`, 2026-11-13).

The broker holds the key: a Sentry user auth token, which you paste into the "Connect
Sentry" item with your organization's slug (`connections.py`). It is read from the
credential store for each request, sent only to the configured Sentry host, and never
appears in a result, an error or the run log.
"""

# system imports
import re
from typing import Any
from urllib.parse import urlsplit

# 3rd party imports
import httpx

# project imports
from maji_mcp.broker.catalog import CallContext, Param, Stub
from maji_mcp.broker.connections import KeyFor, Service, Setting
from maji_mcp.common.protocol import MajiError

VERSION = "1.0"
MAX_PAGES = 20
NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next";\s*results="true"')

# What the key has to be allowed: reading the organization, its projects, and their
# issues and events.
#
PERMISSIONS = ("org:read", "project:read", "event:read")


###############################################################################
#
def service(url: str, transport: httpx.AsyncBaseTransport | None = None) -> Service:
    """
    Sentry, as a service the broker calls with a key you paste.

    Args:
        url: the Sentry host's address, such as `https://sentry.io`, from the
            broker's configuration: the only place the key is sent
        transport: replaces the network, in tests
    """
    base = url.rstrip("/")

    async def check(key: str, settings: dict[str, str]) -> str | None:
        """Read the organization with the key: Sentry takes it, or says why not."""
        async with httpx.AsyncClient(timeout=30, transport=transport) as http:
            try:
                response = await http.get(
                    f"{base}/api/0/organizations/{settings['org']}/",
                    headers={"Authorization": f"Bearer {key}"},
                )
            except httpx.HTTPError as exc:
                return f"Sentry couldn't be reached ({type(exc).__name__})"
        if response.status_code in (401, 403):
            return "Sentry refused the key"
        if response.status_code == 404:
            return f"the key can't read a Sentry organization {settings['org']!r}"
        if response.status_code != 200:
            return f"Sentry answered {response.status_code}"
        return None

    return Service(
        name="sentry",
        label="Sentry",
        host=urlsplit(base).hostname or base,
        sent_as="Authorization: Bearer",
        permissions=PERMISSIONS,
        key_page=f"{base}/settings/account/api/auth-tokens/",
        check=check,
        settings=(
            Setting(
                "org",
                "Organization slug",
                r"[a-z0-9][a-z0-9_-]{0,49}",
                "As in your Sentry address: https://<slug>.sentry.io",
            ),
        ),
        url=base,
    )


class SentryClient:
    """
    A small async client for the Sentry endpoints the stubs use.

    Args:
        url: the Sentry host's address, the only place the key is sent
        key_for: gives the key, and the organization given with it, for each call
        transport: replaces the network, in tests
    """

    def __init__(
        self,
        url: str,
        key_for: KeyFor,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key_for = key_for
        self._http = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/api/0/", timeout=30, transport=transport
        )
        self._project_ids: dict[str, dict[str, str]] = {}

    async def org(self, ctx: CallContext) -> str:
        """The organization given with the key."""
        _, settings = await self._key_for(ctx)
        return settings["org"]

    async def get(
        self, ctx: CallContext, path: str, params: list[tuple[str, str]] | None = None
    ) -> Any:
        body, _ = await self._get(ctx, path, params or [])
        return body

    async def paged(
        self,
        ctx: CallContext,
        path: str,
        params: list[tuple[str, str]],
        max_pages: int,
    ) -> list[Any]:
        """
        Collect every page of a list endpoint, up to `max_pages`.

        `params` is a list of pairs, because Sentry takes a repeated `environment`
        parameter to filter on several environments at once.
        """
        items: list[Any] = []
        url: str | None = path
        for _ in range(max_pages):
            if url is None:
                break
            page, url = await self._get(ctx, url, params)
            params = []
            items.extend(page)
        return items

    async def _get(
        self, ctx: CallContext, url: str, params: list[tuple[str, str]]
    ) -> tuple[Any, str | None]:
        key, _ = await self._key_for(ctx)
        try:
            # httpx replaces a URL's query with `params` even when the list is empty,
            # which would drop the cursor from Sentry's next-page link, so an empty
            # list is passed as None.
            #
            response = await self._http.get(
                url,
                params=tuple(params) if params else None,
                headers={"Authorization": f"Bearer {key}"},
            )
        except httpx.HTTPError as exc:
            raise MajiError(
                "unavailable", f"Sentry unreachable: {type(exc).__name__}"
            ) from None
        path = response.request.url.path
        if response.status_code == 404:
            raise MajiError("not_found", f"Sentry has nothing at {path}")
        if response.status_code == 400:
            raise MajiError(
                "invalid_argument", f"Sentry rejected {path}: {_detail(response)}"
            )
        if response.status_code in (401, 403):
            raise MajiError(
                "unavailable",
                "Sentry refused the key the user connected; they can connect a new "
                "one from the web app",
            )
        if response.status_code == 429:
            raise MajiError(
                "rate_limited",
                "Sentry's rate limit; wait, or make fewer calls at once",
                {"retry_after_s": response.headers.get("Retry-After")},
            )
        if response.status_code >= 400:
            raise MajiError("unavailable", f"Sentry {response.status_code} for {path}")
        match = NEXT_LINK_RE.search(response.headers.get("Link", ""))
        return response.json(), (match.group(1) if match else None)

    async def project_id(self, ctx: CallContext, slug: str) -> str:
        """Return a project's numeric id, which the organization endpoints filter on."""
        org = await self.org(ctx)
        if org not in self._project_ids:
            projects = await self.paged(
                ctx, f"organizations/{org}/projects/", [], MAX_PAGES
            )
            self._project_ids[org] = {p["slug"]: p["id"] for p in projects}
        known = self._project_ids[org]
        if slug not in known:
            raise MajiError(
                "not_found", f"no Sentry project {slug!r}", {"projects": sorted(known)}
            )
        return known[slug]


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", ""))[:200]
    except ValueError:
        return ""


def top_frame(event: dict[str, Any]) -> str | None:
    """
    Return the innermost in-app frame of an event, as `file:line function`.

    Sentry lists frames outermost first, so the last in-app frame is where the error
    surfaced in your own code.
    """
    for entry in event.get("entries", []):
        if entry.get("type") != "exception":
            continue
        for exc in reversed(entry.get("data", {}).get("values") or []):
            frames = (exc.get("stacktrace") or {}).get("frames") or []
            for frame in reversed(frames):
                if frame.get("inApp"):
                    location = frame.get("filename") or frame.get("module") or "?"
                    return f"{location}:{frame.get('lineNo')} {frame.get('function')}"
    return None


###############################################################################
#
def stubs(client: SentryClient) -> list[Stub]:
    """Return the `sentry` stubs, bound to a client."""

    async def projects(args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        org = await client.org(ctx)
        found = await client.paged(ctx, f"organizations/{org}/projects/", [], MAX_PAGES)
        return [
            {
                "slug": p["slug"],
                "id": p["id"],
                "name": p.get("name"),
                "platform": p.get("platform"),
            }
            for p in found
        ]

    async def environments(args: dict[str, Any], ctx: CallContext) -> list[str]:
        org = await client.org(ctx)
        envs = await client.get(ctx, f"projects/{org}/{args['project']}/environments/")
        return [env["name"] for env in envs]

    async def issues(args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        org = await client.org(ctx)
        params = [
            ("project", await client.project_id(ctx, args["project"])),
            ("query", args["query"]),
            ("statsPeriod", args["period"]),
            ("limit", "100"),
        ]
        params += [("environment", env) for env in args["environments"] or []]
        found: list[dict[str, Any]] = await client.paged(
            ctx, f"organizations/{org}/issues/", params, args["max_pages"]
        )
        return found

    async def events(args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        org = await client.org(ctx)
        params = [("full", "true" if args["full"] else "false")]
        found: list[dict[str, Any]] = await client.paged(
            ctx,
            f"organizations/{org}/issues/{args['issue_id']}/events/",
            params,
            args["max_pages"],
        )
        if args["full"]:
            for event in found:
                event["top_frame"] = top_frame(event)
        return found

    async def tag_values(
        args: dict[str, Any], ctx: CallContext
    ) -> list[dict[str, Any]]:
        org = await client.org(ctx)
        tag = await client.get(
            ctx, f"organizations/{org}/issues/{args['issue_id']}/tags/{args['key']}/"
        )
        values: list[dict[str, Any]] = tag.get("topValues", [])
        return values

    return [
        Stub(
            "sentry.projects",
            "The organization's projects.",
            {},
            "list[dict]: each with slug, id, name and platform",
            projects,
        ),
        Stub(
            "sentry.environments",
            "The names of a project's environments.",
            {"project": Param("str", "the project slug")},
            "list[str]",
            environments,
        ),
        Stub(
            "sentry.issues",
            "A project's issues matching a Sentry search, seen within a time window.",
            {
                "project": Param("str", "the project slug"),
                "query": Param(
                    "str",
                    "Sentry search syntax, such as `is:unresolved server_name:worker-*`",
                    "is:unresolved",
                ),
                "period": Param("str", "the window, such as `24h` or `14d`", "14d"),
                "environments": Param(
                    "list[str]", "issues seen in any of these environments", None
                ),
                "max_pages": Param(
                    "int", "stop after this many pages of 100", MAX_PAGES
                ),
            },
            "list[dict]: Sentry's issue objects, with id, title, culprit, firstSeen "
            "and count. count is the issue's events within the period, summed over "
            "the environments given.",
            issues,
            notes="Uses the organization issues endpoint, which treats several "
            "environments as any-of and counts within the period.",
        ),
        Stub(
            "sentry.events",
            "An issue's events, newest first.",
            {
                "issue_id": Param("str", "the numeric issue id"),
                "full": Param(
                    "bool", "include stack traces, and each event's top_frame", False
                ),
                "max_pages": Param("int", "stop after this many pages", 5),
            },
            "list[dict]: Sentry's event objects. With full, each also has top_frame: "
            "the innermost in-app frame as `file:line function`, or None.",
            events,
        ),
        Stub(
            "sentry.tag_values",
            "An issue's most common values for one tag, such as `environment`.",
            {
                "issue_id": Param("str", "the numeric issue id"),
                "key": Param("str", "the tag, such as `environment` or `server_name`"),
            },
            "list[dict]: each with value and count, most common first",
            tag_values,
        ),
    ]
