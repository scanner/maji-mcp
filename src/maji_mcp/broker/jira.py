"""
The `jira` stubs: Jira Cloud's REST API, read-only.

The broker holds the key: a scoped Atlassian API token with the `read:jira-work` scope,
which you paste into the "Connect Jira" item with your site's name and the email of the
account that made the token (`connections.py`). A scoped token works only through
Atlassian's API gateway, `api.atlassian.com/ex/jira/<cloud id>`, so the gateway is the
only host the key is sent to, by HTTP basic auth with the email. The site's cloud id
comes from the site's public `/_edge/tenant_info`, which is read without the key.

Jira's own answers nest every field and hold text in the Atlassian Document Format.
The stubs return a record for each issue instead: names in place of nested objects, and
plain text in place of documents. A script asks for any other field by its id.
"""

# system imports
import re
from collections.abc import Sequence
from typing import Any

# 3rd party imports
import httpx

# project imports
from maji_mcp.broker.catalog import CallContext, Param, Stub
from maji_mcp.broker.connections import KeyFor, Service, Setting
from maji_mcp.common.protocol import MajiError

VERSION = "1.0"
API_URL = "https://api.atlassian.com"
SITE_URL = "https://{site}.atlassian.net"
PAGE = 100
LIMIT = 1000

# What the key has to be allowed: reading issues, projects and their fields.
#
PERMISSIONS = ("read:jira-work",)

# A site's name, as in `<site>.atlassian.net`; a cloud id, as tenant_info gives it; and
# an issue's key or numeric id. Each is checked before it goes into an address.
#
SITE = r"[a-z0-9][a-z0-9-]{0,62}"
CLOUD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ISSUE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,254}-[0-9]{1,18}|[0-9]{1,18}")

# The fields every issue record is built from.
#
FIELDS = (
    "summary",
    "status",
    "issuetype",
    "priority",
    "assignee",
    "reporter",
    "created",
    "updated",
    "resolutiondate",
    "resolution",
    "labels",
    "components",
    "fixVersions",
    "parent",
    "project",
)

# The ways a key is refused at the gateway, which the check names for you.
#
REFUSED = (
    "Atlassian refused the key with that email. It needs to be a scoped API token "
    "with read:jira-work, made by the account whose email you gave, and not expired"
)


###############################################################################
#
def service(
    api_url: str = API_URL,
    site_url: str = SITE_URL,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Service:
    """
    Jira Cloud, as a service the broker calls with a key you paste.

    Args:
        api_url: Atlassian's API gateway, the only place the key is sent
        site_url: a site's address, with `{site}` for its name; its tenant_info is
            read without the key
        transport: replaces the network, in tests
    """
    api = api_url.rstrip("/")

    async def check(key: str, settings: dict[str, str]) -> str | None:
        """Read one project with the key: the gateway takes it, or says why not."""
        async with httpx.AsyncClient(timeout=30, transport=transport) as http:
            try:
                cloud = await cloud_id(http, site_url, settings["site"])
                if cloud is None:
                    return f"there's no Jira site {settings['site']}.atlassian.net"
                response = await http.get(
                    f"{api}/ex/jira/{cloud}/rest/api/3/project/search",
                    params={"maxResults": "1"},
                    auth=(settings["email"], key),
                )
            except httpx.HTTPError as exc:
                return f"Atlassian couldn't be reached ({type(exc).__name__})"
        if response.status_code in (401, 403):
            return REFUSED
        if response.status_code != 200:
            return f"Jira answered {response.status_code}"
        return None

    return Service(
        name="jira",
        label="Jira",
        host=httpx.URL(api).host,
        sent_as="Authorization: Basic, with your email",
        permissions=PERMISSIONS,
        key_page="https://id.atlassian.com/manage-profile/security/api-tokens",
        check=check,
        settings=(
            Setting(
                "site",
                "Site",
                SITE,
                "As in your Jira address: https://<site>.atlassian.net",
            ),
            Setting(
                "email",
                "Email",
                r"[^@\s]{1,64}@[^@\s]{1,253}",
                "Of the Atlassian account that made the key",
            ),
        ),
        url=api,
    )


async def cloud_id(http: httpx.AsyncClient, site_url: str, site: str) -> str | None:
    """
    Read a site's cloud id from its public tenant_info, without a key.

    Returns:
        The cloud id, or None when there is no such site: Atlassian answers 404 for a
        name no site has.
    """
    response = await http.get(site_url.format(site=site) + "/_edge/tenant_info")
    if response.status_code != 200:
        return None
    try:
        found = response.json().get("cloudId")
    except ValueError, AttributeError:
        return None
    return found if isinstance(found, str) and CLOUD_ID.fullmatch(found) else None


class JiraClient:
    """
    A small async client for the Jira endpoints the stubs use.

    Args:
        key_for: gives the key, and the site and email given with it, for each call
        api_url: Atlassian's API gateway, the only place the key is sent
        site_url: a site's address, with `{site}` for its name
        transport: replaces the network, in tests
    """

    def __init__(
        self,
        key_for: KeyFor,
        api_url: str = API_URL,
        site_url: str = SITE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key_for = key_for
        self._api = api_url.rstrip("/")
        self._site_url = site_url
        self._http = httpx.AsyncClient(timeout=30, transport=transport)
        self._clouds: dict[str, str] = {}

    async def request(
        self,
        ctx: CallContext,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """
        Make one request to the site's REST API, at `path` under `rest/api/3/`.

        Raises:
            MajiError: the contract's code for what Jira answered, in words that
                never quote the request
        """
        key, settings = await self._key_for(ctx)
        base = await self._base(settings["site"])
        try:
            response = await self._http.request(
                method,
                base + path,
                params=params,
                json=body,
                auth=(settings["email"], key),
            )
        except httpx.HTTPError as exc:
            raise MajiError(
                "unavailable", f"Jira unreachable: {type(exc).__name__}"
            ) from None
        status = response.status_code
        if status == 400:
            raise MajiError(
                "invalid_argument", f"Jira rejected {path}: {_messages(response)}"
            )
        if status in (401, 403):
            raise MajiError(
                "unavailable",
                "Jira refused the key the user connected, which may have expired; "
                "they can connect a new one from the web app",
            )
        if status == 404:
            raise MajiError(
                "not_found", f"Jira has nothing at {path}, or the key can't see it"
            )
        if status == 429:
            raise MajiError(
                "rate_limited",
                "Jira's rate limit; wait, or make fewer calls at once",
                {"retry_after_s": response.headers.get("Retry-After")},
            )
        if status >= 300 or status < 200:
            raise MajiError("unavailable", f"Jira {status} for {path}")
        return response.json()

    async def _base(self, site: str) -> str:
        """The site's REST API on the gateway, its cloud id read once."""
        if site not in self._clouds:
            try:
                cloud = await cloud_id(self._http, self._site_url, site)
            except httpx.HTTPError as exc:
                raise MajiError(
                    "unavailable", f"Jira site unreachable: {type(exc).__name__}"
                ) from None
            if cloud is None:
                raise MajiError(
                    "unavailable",
                    f"there's no Jira site {site}.atlassian.net; the user can "
                    "connect Jira again with the right site",
                )
            self._clouds[site] = cloud
        return f"{self._api}/ex/jira/{self._clouds[site]}/rest/api/3/"

    async def offsets(
        self, ctx: CallContext, path: str, field: str, params: dict[str, str]
    ) -> list[Any]:
        """
        Collect every page of an endpoint paged by `startAt`, as the lists under
        `field` in each page.
        """
        items: list[Any] = []
        while True:
            page = await self.request(
                ctx,
                "GET",
                path,
                {**params, "startAt": str(len(items)), "maxResults": str(PAGE)},
            )
            found = page.get(field) or []
            items.extend(found)
            total = page.get("total")
            done = page.get("isLast")
            if done is None:
                done = total is None or len(items) >= total
            if done or not found:
                return items


def _messages(response: httpx.Response) -> str:
    """Jira's own reasons for refusing a request, such as a JQL error."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    found = [str(m) for m in body.get("errorMessages") or []]
    found += [f"{k}: {v}" for k, v in (body.get("errors") or {}).items()]
    return "; ".join(found)[:500]


###############################################################################
#
def adf_text(doc: Any) -> str:
    """
    Render an Atlassian Document Format document as plain text.

    Paragraphs and headings become lines with a blank line between, list items become
    `-` or numbered lines, table rows become cells joined by `|`, mentions become the
    name shown, and links to cards become their address. Formatting is dropped.
    """

    def walk(node: Any) -> str:
        if isinstance(node, list):
            return "".join(walk(n) for n in node)
        if not isinstance(node, dict):
            return ""
        kind = node.get("type")
        attrs = node.get("attrs") or {}
        content = node.get("content") or []
        if kind == "text":
            return str(node.get("text", ""))
        if kind == "hardBreak":
            return "\n"
        if kind in ("mention", "status"):
            return str(attrs.get("text", ""))
        if kind == "emoji":
            return str(attrs.get("text") or attrs.get("shortName", ""))
        if kind == "date":
            return str(attrs.get("timestamp", ""))
        if kind in ("inlineCard", "blockCard", "embedCard"):
            return str(attrs.get("url", "")) + ("\n\n" if kind != "inlineCard" else "")
        if kind == "rule":
            return "---\n\n"
        if kind in ("bulletList", "orderedList"):
            start = int(attrs.get("order", 1) or 1)
            lines = []
            for n, item in enumerate(content):
                mark = f"{start + n}." if kind == "orderedList" else "-"
                text = walk(item.get("content") or []).strip()
                text = re.sub(r"\n\s*\n", "\n", text).replace("\n", "\n  ")
                lines.append(f"{mark} {text}")
            return "\n".join(lines) + "\n\n"
        if kind == "tableRow":
            return " | ".join(walk(cell).strip() for cell in content) + "\n"
        if kind == "table":
            return walk(content) + "\n"
        inner = walk(content)
        if kind in ("paragraph", "heading", "codeBlock", "blockquote", "panel"):
            return inner.rstrip("\n") + "\n\n"
        return inner

    text = walk(doc)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _name(value: Any) -> str | None:
    return value.get("name") if isinstance(value, dict) else None


def _person(value: Any) -> str | None:
    return value.get("displayName") if isinstance(value, dict) else None


def _value(value: Any) -> Any:
    """
    A field's value for a script: a document as text, a user as their name, an option
    as its value, and anything else as Jira gives it.
    """
    if isinstance(value, list):
        return [_value(v) for v in value]
    if not isinstance(value, dict):
        return value
    if value.get("type") == "doc":
        return adf_text(value)
    if "accountId" in value:
        return value.get("displayName")
    if "value" in value and "name" not in value:
        return value["value"]
    return value


def record(issue: dict[str, Any], extra: Sequence[str] = ()) -> dict[str, Any]:
    """
    An issue as a script reads it: names in place of Jira's nested objects.

    Args:
        issue: the issue as Jira gives it
        extra: the ids of other fields to carry, under `fields`
    """
    fields = issue.get("fields") or {}
    status = fields.get("status") or {}
    found: dict[str, Any] = {
        "key": issue.get("key"),
        "id": issue.get("id"),
        "project": (fields.get("project") or {}).get("key"),
        "type": _name(fields.get("issuetype")),
        "summary": fields.get("summary"),
        "status": status.get("name"),
        "status_category": (status.get("statusCategory") or {}).get("key"),
        "priority": _name(fields.get("priority")),
        "assignee": _person(fields.get("assignee")),
        "reporter": _person(fields.get("reporter")),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "resolved": fields.get("resolutiondate"),
        "resolution": _name(fields.get("resolution")),
        "labels": fields.get("labels") or [],
        "components": [_name(c) for c in fields.get("components") or []],
        "fix_versions": [_name(v) for v in fields.get("fixVersions") or []],
        "parent": (fields.get("parent") or {}).get("key"),
    }
    if extra:
        found["fields"] = {name: _value(fields.get(name)) for name in extra}
    return found


def _linked(link: dict[str, Any]) -> dict[str, Any]:
    """One of an issue's links, from the issue's side: how it relates, and to what."""
    kind = link.get("type") or {}
    outward = "outwardIssue" in link
    other = link.get("outwardIssue" if outward else "inwardIssue") or {}
    fields = other.get("fields") or {}
    return {
        "relation": kind.get("outward" if outward else "inward"),
        "key": other.get("key"),
        "summary": fields.get("summary"),
        "status": _name(fields.get("status")),
    }


def _comment(comment: dict[str, Any]) -> dict[str, Any]:
    return {
        "author": _person(comment.get("author")),
        "created": comment.get("created"),
        "updated": comment.get("updated"),
        "text": adf_text(comment.get("body")),
    }


def _issue_key(value: Any) -> str:
    if not isinstance(value, str) or not ISSUE.fullmatch(value):
        raise MajiError(
            "invalid_argument", f"{value!r} isn't an issue key, such as ABC-123"
        )
    return value


def _jql(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MajiError(
            "invalid_argument", "jql is a JQL query, such as `project = ABC`"
        )
    return value


def _field_ids(value: list[Any] | None) -> list[str]:
    ids = list(value or [])
    if not all(isinstance(f, str) and re.fullmatch(r"[\w.-]{1,100}", f) for f in ids):
        raise MajiError(
            "invalid_argument",
            "fields are field ids, such as `customfield_10016`: jira.fields() "
            "lists them",
        )
    return ids


###############################################################################
#
def stubs(client: JiraClient) -> list[Stub]:
    """Return the `jira` stubs, bound to a client."""

    async def projects(args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        found = await client.offsets(ctx, "project/search", "values", {})
        return [
            {
                "key": p.get("key"),
                "name": p.get("name"),
                "id": p.get("id"),
                "type": p.get("projectTypeKey"),
            }
            for p in found
        ]

    async def fields(args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        found = await client.request(ctx, "GET", "field")
        return [
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "custom": bool(f.get("custom")),
                "type": (f.get("schema") or {}).get("type"),
            }
            for f in found
        ]

    async def issues(args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        extra = _field_ids(args["fields"])
        limit = max(1, args["limit"] or LIMIT)
        params = {
            "jql": _jql(args["jql"]),
            "fields": ",".join([*FIELDS, *extra]),
            "maxResults": str(min(PAGE, limit)),
        }
        found: list[dict[str, Any]] = []
        while len(found) < limit:
            page = await client.request(ctx, "GET", "search/jql", params)
            found.extend(record(i, extra) for i in page.get("issues") or [])
            token = page.get("nextPageToken")
            if page.get("isLast") or not token or not page.get("issues"):
                break
            params["nextPageToken"] = token
        return found[:limit]

    async def count(args: dict[str, Any], ctx: CallContext) -> int:
        found = await client.request(
            ctx, "POST", "search/approximate-count", body={"jql": _jql(args["jql"])}
        )
        return int(found.get("count", 0))

    async def issue(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        key = _issue_key(args["key"])
        extra = _field_ids(args["fields"])
        wanted = [*FIELDS, "description", "comment", "issuelinks", "subtasks", *extra]
        found = await client.request(
            ctx, "GET", f"issue/{key}", {"fields": ",".join(wanted)}
        )
        details = found.get("fields") or {}

        # Jira gives the first comments with the issue. Past those, the rest come
        # from the issue's comment pages.
        #
        thread = details.get("comment") or {}
        comments = (
            (thread.get("comments") or []) if isinstance(thread, dict) else thread
        )
        if isinstance(thread, dict) and (thread.get("total") or 0) > len(comments):
            comments = await client.offsets(ctx, f"issue/{key}/comment", "comments", {})
        return {
            **record(found, extra),
            "description": adf_text(details.get("description")),
            "comments": [_comment(c) for c in comments],
            "links": [_linked(link) for link in details.get("issuelinks") or []],
            "subtasks": [
                {
                    "key": s.get("key"),
                    "summary": (s.get("fields") or {}).get("summary"),
                    "status": _name((s.get("fields") or {}).get("status")),
                }
                for s in details.get("subtasks") or []
            ],
        }

    async def changelog(args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        key = _issue_key(args["key"])
        only = {f.lower() for f in args["fields"] or []}
        histories = await client.offsets(ctx, f"issue/{key}/changelog", "values", {})
        return [
            {
                "at": history.get("created"),
                "author": _person(history.get("author")),
                "field": item.get("field"),
                "from": item.get("fromString"),
                "to": item.get("toString"),
            }
            for history in histories
            for item in history.get("items") or []
            if not only or str(item.get("field", "")).lower() in only
        ]

    limit_note = (
        "Returns at most `limit` issues: as many as that means there may be more, so "
        "narrow the query, or count first with jira.count."
    )
    return [
        Stub(
            "jira.projects",
            "The projects the key can see.",
            {},
            "list[dict]: each with key, name, id and type",
            projects,
        ),
        Stub(
            "jira.fields",
            "Every field the site has, to find a custom field's id by its name.",
            {},
            "list[dict]: each with id, name, custom and type",
            fields,
        ),
        Stub(
            "jira.issues",
            "The issues matching a JQL query, as records.",
            {
                "jql": Param(
                    "str",
                    "JQL, such as `project = ABC AND updated >= -7d ORDER BY updated "
                    "DESC`",
                ),
                "fields": Param(
                    "list[str]",
                    "the ids of other fields to carry, such as `customfield_10016`",
                    None,
                ),
                "limit": Param("int", "stop after this many issues", LIMIT),
            },
            "list[dict]: each with key, id, project, type, summary, status, "
            "status_category (`new`, `indeterminate` or `done`), priority, assignee, "
            "reporter, created, updated, resolved, resolution, labels, components, "
            "fix_versions and parent; and fields, by id, when asked for. People are "
            "their names, and times are ISO 8601 as Jira gives them.",
            issues,
            notes=f"{limit_note} Jira refuses a query with no condition; add one, "
            "such as a project. A rich-text field comes as text, a user as their "
            "name, and an option as its value.",
        ),
        Stub(
            "jira.count",
            "About how many issues match a JQL query, without reading them.",
            {"jql": Param("str", "JQL, as for jira.issues")},
            "int",
            count,
            notes="Jira's count is approximate: issues changed in the last few "
            "seconds may be missing from it.",
        ),
        Stub(
            "jira.issue",
            "One issue, with its description, comments, links and subtasks.",
            {
                "key": Param("str", "the issue's key, such as `ABC-123`"),
                "fields": Param("list[str]", "the ids of other fields to carry", None),
            },
            "dict: the record jira.issues gives, with description (text), comments "
            "(each with author, created, updated and text), links (each with "
            "relation, key, summary and status) and subtasks (each with key, summary "
            "and status)",
            issue,
        ),
        Stub(
            "jira.changelog",
            "An issue's history of changes, oldest first.",
            {
                "key": Param("str", "the issue's key, such as `ABC-123`"),
                "fields": Param(
                    "list[str]",
                    "only changes to these fields, by name, such as `status`",
                    None,
                ),
            },
            "list[dict]: one for each field changed, with at, author, field, from "
            "and to",
            changelog,
            notes="To find the issues that changed, query first: `status CHANGED "
            "DURING (-7d, now())` in jira.issues.",
        ),
    ]
