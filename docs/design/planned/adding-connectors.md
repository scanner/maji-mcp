# Planned: adding connectors

How you will give scripts a service that maji-mcp has no built-in stubs for: a REST
API, an MCP server, a database, or code of your own. A **connector** tells the broker
how to reach one service: what the broker talks to, where, with which credential, and
which operations scripts may call.

**Status:** planned. Nothing in this document is built. Today scripts reach Sentry,
Jira and GitHub through built-in stubs with keys you paste
([../connections.md](../connections.md)), and MCP servers listed in
`config/servers.json` ([../broker.md](../broker.md)). This document is the guide as
it will read once connectors are built, so it is written in the present tense.
[connectors.md](connectors.md) says how connectors work inside maji-mcp: the
mechanics, the REST API, what a connector provides and what a client does.

## What every connector has in common

- **Nothing is reachable until you approve it.** Adding a connector raises an item in
  your queue. The item shows what scripts will be able to call, where requests go,
  what runs on your machine, and which principals may use the connector. Approving a
  connector takes your passkey, every time.
- **Anyone may propose; only you approve.** The web app, the command line, the REST
  API and an agent can all propose a connector. Each of them raises the same item,
  and only you, signed in to the web app, approve the item.
- **Scripts only read.** Each operation is a read or a write, and the broker refuses
  writes until changesets are built ([writes.md](writes.md)). What enforces read-only
  depends on the kind of connector, and each kind's section says what does.
- **A connector is one namespace.** The connector's operations appear in `list_api`
  as `<namespace>.<operation>`, such as `tripsy.trips_list`, under the same policy,
  rate limits and run log as every other stub.
- **Your connectors stay yours.** A connector, its definition and the skills written
  for it live in your instance or in your own repository. None of them goes into
  maji-mcp's repository.
- **Removing a connector takes effect at once,** and forgets the connector's key.
  Removing needs no passkey, because removing only takes access away.

## Choosing the kind

| You want to reach | Kind | What runs on your Mac | Glue you write |
|---|---|---|---|
| Data a program on your Mac can save as a file, such as a DuckDB or SQLite database | [no connector: a mount](#data-in-files-on-your-mac) | the program that saves the file | none, or skills |
| Data a credential can copy as files, such as objects in a cloud bucket, or a slice of a large dataset that your group's own library knows how to find | [a fetcher](#data-copied-into-a-cache) | a credential helper; the fetcher runs in a container of its own | a fetcher, and skills |
| A REST API that takes a key | [a REST API](#a-rest-api) | nothing | skills |
| An MCP server on the internet | [an MCP server on the internet](#an-mcp-server-on-the-internet) | nothing | none, or skills that reshape results |
| An MCP server an app serves on your Mac | [an MCP server on your Mac](#an-mcp-server-on-your-mac) | the app, which you run anyway | none, or skills |
| An MCP server that runs as a command, over stdio | [an MCP server you start as a command](#an-mcp-server-you-start-as-a-command) | the command, started by the launcher | none, or skills |
| A SQL database | [a database](#a-database) | nothing, or a tunnel or a credential helper | skills |
| Anything that needs your Mac's code, network or identity throughout | [code of your own](#code-of-your-own) | your code, started by the launcher | a small MCP server, and skills |

When a service offers more than one way in, choose in this order:

1. **A file,** when the data can be copied as one: a file a program on your Mac
   saves, or one a fetcher copies with a credential. The script reads the file
   through a mount, with no network, and gets the whole of DuckDB or SQLite with no
   limit on rows.
2. **The vendor's MCP server,** when there is one. The vendor keeps the server up to
   date, and the server's tool descriptions carry what an agent needs to know about
   the data.
3. **A REST definition beside it,** for what the MCP server leaves out, or for reading
   in bulk.
4. **A database definition** whenever the broker can reach the database. The broker
   then enforces read-only itself.
5. **Code of your own** only when your Mac's code, network or identity is needed for
   more than the credential or the route. A credential helper or a tunnel covers those
   two alone.

A service can have several connectors, each with its own namespace: `tripsy` for
Tripsy's MCP server and `tripsy_api` for Tripsy's REST API. The Connections page shows
them together under the service.

## Data in files on your Mac

This way in works today, with no connector ([../runner.md](../runner.md)). When a
program on your Mac can save the data you want as a database file, a script reads the
file through a mount:

1. **The program saves a snapshot** into a folder of its own, such as
   `~/tmp/archive-snapshots/`, under a new name each time, and moves a link in the folder
   to the newest snapshot. Log captures are published the same way.
2. **A script asks for the folder** in `run_python`'s `paths`. You grant the folder
   once.
3. **The script opens the file read-only:**

   ```python
   import duckdb

   db = duckdb.connect("/mnt/archive-snapshots/kx12-2026-10.duckdb", read_only=True)
   print(db.sql("SELECT sensor, count(*) FROM readings GROUP BY sensor").fetchall())
   ```

   A SQLite file opens with `maji.sqlite`.

What makes it work:
- **The file has to hold its data.** The sandbox has no network, so a view that
  reads from a bucket or a URL fails: DuckDB tries to install its `httpfs` extension,
  and can't. The program that saves the snapshot saves each such view as a table.
- **The runtime's DuckDB has to read the file's format.** The runtime has DuckDB 2.0,
  which reads files DuckDB 1.4 wrote.
- **Nothing limits the rows** except the run's memory and time. The script counts,
  joins and groups with the whole of DuckDB, and prints only the answer.

**Who saves the snapshot:** you; an agent with a shell, such as Claude Code; or a
tool of [code of your own](#code-of-your-own) that saves one on request. A script that
asked for a snapshot reads it in the next script, which mounts the folder.

## Three ways to add a connector

- **The web app.** Open **Connections** and choose **Add a connector**. Paste a URL, a
  path or a definition. The web app says which kind it takes the input to be, and lets
  you change the kind.
- **The command line.** `maji-connectors add <url, path or file>` reads its input the
  same way, and `--as <kind>` overrides the guess. The command uses the client token
  `make setup` made.
- **The REST API.** `POST /api/v0/connectors`, with a client token:

  ```sh
  curl -H "Authorization: Bearer $(cat ~/.config/maji-mcp/secrets/control-cli.token)" \
    -H "Content-Type: application/json" \
    -d '{"mcp": "https://mcp.tripsy.app"}' \
    http://localhost:8767/api/v0/connectors
  ```

Each way reads its input by the same rules:

| You give | Taken as |
|---|---|
| `http://localhost:<port>/…` or `http://127.0.0.1:<port>/…` | an MCP server on your Mac |
| a URL on a git host, such as `https://github.com/…`, or a URL ending in `.git` | code of your own, from a repository |
| any other `https://` URL | an MCP server on the internet |
| a path to a folder | code of your own, from a folder that holds `maji-connector.toml` |
| a path to a `.json` file, or pasted JSON | a REST or database definition, by the definition's top-level key |
| a command, with `--as command` | an MCP server you start as a command |

**A folder, a repository or a command needs the launcher.** Only a program on your
Mac can read your folders and private repositories, so the launcher reads them
([An MCP server you start as a command](#an-mcp-server-you-start-as-a-command)). Start
it with `make connectors`. Until the launcher is running, the item waits and says
that it is waiting for the launcher.

**An address waits for you.** maji-mcp contacts an MCP server's address or a
repository's URL only for you. One you add in the web app is looked up at once. One
proposed from the command line, the REST API or an agent raises an item that shows
only the address, until you choose **Look up** on it in the web app. An address
could be anything the broker or your Mac can reach, including hosts on your
company's network over the VPN.

You can also ask an agent to do all of it ([Letting an agent write a
connector](#letting-an-agent-write-a-connector)).

## A REST API

For a service with a REST API and a key, such as Tripsy's API at
`https://api.tripsy.app`.

**1. Write a definition.** A REST definition is a few lines of JSON, with no code:

```json
{
  "rest": {
    "namespace": "tripsy_api",
    "label": "Tripsy API",
    "url": "https://api.tripsy.app",
    "key": {"header": "Authorization", "format": "Token {key}"},
    "check": "/v1/me",
    "read": {"GET": ["/v1/", "/v2/"]}
  }
}
```

| Field | Meaning |
|---|---|
| `namespace` | what scripts call the connector |
| `url` | where requests go, and the only host the key is sent to |
| `key` | how each request carries the key: a header and its format, or a query parameter |
| `check` | a path that answers 200 for a working key, read once when you connect |
| `read` | the requests scripts may make: each method, with the path prefixes it may reach. GET is a read. A POST belongs here only for an endpoint that reads, such as a search. |
| `settings` | optional: values given with the key, none of them secret, such as an organization. Each has a pattern the value must match. A `{name}` in `url` is filled in from the setting. |
| `key_page` | optional: where you make a key |
| `permissions` | optional: what the key needs, in the service's own terms |

**2. Add it.** Paste the definition into **Add a connector**, or run
`maji-connectors add tripsy-api.json`.

**3. Approve it, and paste the key.** The item shows the host the key will be sent
to, the requests scripts may make, and the principals that may call the connector.
A key sent to a new host makes the item high risk (`data_leaves`). Paste the key into
the same item. The broker reads `check` with the key before keeping the key.

**4. Scripts call it.** The namespace has one operation for each method in `read`:

```python
page = tripsy_api.get("/v1/categories")
page["status"]           # 200
page["body"]["results"]  # parsed, since the body is JSON
```

`get(path, params=None)` returns the status, the response headers and the body. The
path may also be a full URL on the same host, such as a page's `next` link.

**5. Write skills for what you use.** A skill turns the raw API into what an agent
wants: the skill follows the pages, keeps the fields worth having, and stops at a cap.
A skill that stops at its cap says so, because an analysis of a list cut short gives
a wrong answer with no sign of it. A service's own habits go in the skill too. Most
Tripsy lists come in pages of `count`, `next`, `previous` and `results`, but
`/v1/trips` and `/v1/categories` return `results` alone:

```python
def tripsy_all(path: str, limit: int = 1000) -> list[dict]:
    """Every record a Tripsy list returns, following its pages."""
    found, following = [], path
    while following:
        body = tripsy_api.get(following)["body"]
        found += body["results"]
        following = body.get("next")
        if following and len(found) >= limit:
            raise ValueError(f"{path}: more than {limit} records; narrow the list")
    return found
```

Keep the skill with `save_skill`. A kept skill runs at once for the agent that kept
it. Promote the skill to share it with your other agents ([../skills.md](../skills.md)).

**What enforces read-only:** the broker refuses every method and path the definition
does not list. Tripsy's tokens can also write, so the definition's list is the only
limit.

**What the broker does for every request:**
- adds the key, and sends the key only to the host in `url`
- follows no redirects, and refuses a full URL on another host
- removes the key from any response that echoes the key back
- refuses a response larger than its cap, with an error that names the cap, and never
  returns part of a body

**When a definition is not enough:** a service that needs a request before the first
one, such as Jira's lookup of a site's cloud id; signing in with OAuth; or requests
that must be signed, such as AWS's. Use code of your own for those.

## An MCP server on the internet

For a vendor's hosted MCP server, such as Tripsy's at `https://mcp.tripsy.app`.

**1. Add the server's URL.**

**2. Read the item.** The item lists every tool the server offers, each with:
- its parameters
- its description, labelled as the server's own words, since every agent will read
  the description
- a proposed read or write. A tool the server marks read-only (`readOnlyHint`) is
  proposed as a read, and every other tool as a write.

Change any classification before you approve, and choose the namespace; the server's
own name is suggested. Choose the principals that may call the connector.

**3. Approve, and sign in.** Most hosted servers sign in with OAuth: approving sends
your browser to the vendor's sign-in page, and the broker keeps the token
([connections.md](connections.md)). A server that takes a key instead asks for the
key in the item.

**4. Scripts call each read tool as a stub,** such as `tripsy.trips_list()`. A prefix
every tool shares, such as `tripsy_`, is dropped. A tool that returns structured
content returns records; any other tool returns its text, which a skill can turn into
records.

**What enforces read-only:** the classification you approved. Tripsy's tokens can't
be limited to reading, so the classification is the only limit. `tripsy_raw_request`,
which Tripsy marks as not read-only, stays a write the broker refuses.

**When the server changes its tools,** a new item shows the change. Until you approve
the change, scripts can call only the tools that did not change.

## An MCP server on your Mac

For an app that serves MCP on your Mac's localhost, such as
`tripsy-mcp --transport http --http-addr 127.0.0.1:8787`, or an app that serves MCP
while the app is open.

**1. Add the server's URL,** such as `http://localhost:8787/mcp`. The broker reaches
your Mac's localhost from its container.

**2. Approve it,** as for a server on the internet.

**3. Paste a key, if the server takes one.** Tripsy's local HTTP server takes a Tripsy
token on every call, and ignores the token in your Keychain, so the item asks for a
token.

**The broker calls itself `localhost`.** Many local servers guard against DNS
rebinding by answering only requests addressed to `localhost`. The broker reaches
your Mac at `host.docker.internal`, so it sends `Host: localhost:<port>`.

**An app may have to let other programs in.** Some apps answer only callers inside
the app until a setting lets others in. Turn the setting on before you add the server.

**A server may cap the rows each call returns.** A script pages with the tool's own
offset, and counts and groups in the query, so only the answer comes back.

**The app has to be running.** A call while the app is closed returns `unavailable`.
An app that picks a new port each time the app starts breaks the connector: edit the
connector's address, which raises an item that shows only the new port.

## An MCP server you start as a command

For an MCP server that speaks stdio, such as `tripsy-mcp` started with no flags, or
emacs-org-mcp.

**1. Start the launcher,** with `make connectors`. The launcher is the program on your
Mac that runs the commands you approved for connectors: MCP servers you start as
commands, code of your own, tunnels and credential helpers. The launcher runs as you,
and runs nothing you have not approved. `make connectors-status` shows what the
launcher is running.

**2. Add the command:** `maji-connectors add --as command -- tripsy-mcp`, or choose
**A command** in the web app.

**3. Approve it, in two steps.** A stdio server's tools can be listed only by running
the server.
1. The first item asks to run the command. The item shows the full command line,
   every argument included, the program's path and a hash of the program, the
   folder the program runs in, and the environment variables it gets.
2. Once you approve, the launcher starts the program behind a bridge, and a second
   item lists the program's tools, as for any MCP server.

**4. The command uses what you have.** `tripsy-mcp` signs in with the Tripsy token in
your Keychain, so maji-mcp holds no key for it.

**What you are trusting:** the program itself, which can do anything you can do. The
classification limits what scripts can call, not what the program does. When the
program changes, after an update for instance, the launcher stops the program and
raises an item that shows the old and new versions.

## A database

For a SQL database, starting with PostgreSQL. The example is a made-up shop's
database, read from a replica.

**1. Write a definition:**

```json
{
  "database": {
    "namespace": "shop",
    "label": "Shop database (read replica)",
    "engine": "postgresql",
    "host": "shop-ro.db.example.com",
    "port": 5432,
    "database": "shop",
    "user": "readonly",
    "limits": {"rows": 50000, "seconds": 30}
  }
}
```

**2. Add it, approve it, and paste the password.** The item shows where queries go,
the user, and the limits. Connecting checks that the broker can reach the database
and sign in.

**3. Scripts query it:**

```python
rows = shop.query(
    "SELECT status, count(*) AS n FROM sales.orders GROUP BY status"
)
shop.tables(schema="sales")    # the schema's tables
shop.columns("sales.orders")   # one table's columns and types
```

`query(sql, params=None)` returns the rows as records. Pass values in `params`, never
in the SQL text. A query that would return more rows than the row limit fails with
`invalid_argument`, naming the limit, and returns no rows: count or group in SQL, or
add a `LIMIT` of your own.

**4. Keep what you know about the data in skills:** which schema holds what, what
the columns mean, and which queries are safe on the largest tables.
That knowledge stays in your library.

**What enforces read-only:** every query runs in a read-only transaction and stops at
the time limit. Use a database user that can only read as well. The user's permissions are the guarantee the database itself gives.

### Reaching the database

- **Over your VPN.** Containers on your Mac send their traffic through your Mac, so a
  database your Mac reaches over a VPN may be reachable from the broker too.
  Connecting tells you whether the broker could reach the database.
- **Through a tunnel.** For a database behind a bastion, SSM or `kubectl`, the
  launcher keeps a tunnel up, and the broker connects to the tunnel's port on your
  Mac. Add the tunnel's command to the definition:

  ```json
  "tunnel": ["kubectl", "--context", "example-prod", "port-forward",
             "-n", "data", "svc/shop-ro", "15432:5432"]
  ```

- **With a short-lived token instead of a password.** A credential helper is a command
  on your Mac that prints a password, such as an RDS IAM token, which lasts 15
  minutes:

  ```json
  "password_from": ["aws", "rds", "generate-db-auth-token",
                    "--hostname", "shop-ro.db.example.com", "--port", "5432",
                    "--username", "readonly", "--profile", "example-ro"]
  ```

  The launcher runs the helper when the broker needs a password, and the broker uses
  the password for that connection alone. When your SSO sign-in has expired, a query
  returns `unavailable` and says so.

A tunnel and a credential helper are commands that run as you, so they are approved
with the definition, and the item shows each of them. The launcher runs them, so
`make connectors` has to be running.

### What becomes of the rows

- **Rows stay in the sandbox.** Only what a script prints reaches the agent.
- **The broker writes no rows to disk.** A saved value lives only in the broker's
  memory, and is dropped when its session has gone an hour unused, or sooner when
  memory runs short (`src/maji_mcp/broker/store.py`). The run log records sizes,
  never results.
- **Whether an agent may read a database's data is your organization's decision.**
  maji-mcp enforces the limits you set, and does not make that decision for you.

## Code of your own

For anything the other kinds can't reach: a service only your company's own library
knows how to reach, a protocol maji-mcp doesn't speak, or requests that must be
signed. Code of your own is a small MCP server, in a folder or a repository of its
own.

**1. Start the launcher,** with `make connectors`, if it is not running already. The
launcher reads the connector file, and later runs the connector.

**2. Write it.** `maji-connectors new inventory` copies a template: a connector file,
a FastMCP server, a read-only database helper, and a test against a fake. The
**connector file**, `maji-connector.toml`, says what the server is and what each tool
does:

```toml
namespace = "inventory"
version = "0.1"
run = "inventory-connector"   # a console script in the folder's pyproject.toml

[tools]
stock = "read"
locations = "read"
```

The server is ordinary FastMCP code. Typed parameters become the stub's parameters,
and a typed return value becomes structured results:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("inventory")


@mcp.tool()
def stock(sku: str) -> list[dict]:
    """The stock of one item, by location."""
    ...
```

**3. Add it.**
- **A folder, while you try it out:** `maji-connectors add ~/src/inventory-connector`.
- **A repository, once it is a project:**
  `maji-connectors add https://github.com/example/inventory-connector@v0.1`. The
  branch or tag is resolved to a commit when you add it, and the connector stays on
  that commit.

**4. Approve it.** The item shows the connector file, the command the launcher will
run, and, for a repository, the commit. Nothing from the folder or the repository
runs before you approve: the connector file is read without installing anything.

**5. The launcher runs it,** behind a bridge: with `uv run` in a folder, or with
`uvx` from the repository at the approved commit.

**Updating it:**
- **A folder:** the code is not pinned. Edit the code, then run
  `maji-connectors restart inventory`. A change to the connector file raises an item.
- **A repository:** `maji-connectors update inventory` resolves the branch or tag
  again, and the item shows the commits and the diff since the approved commit.

**What you are trusting:** your code, which runs as you. The tool classification
limits what scripts can call; only your code decides what a tool called a read
actually does. For SQL, use a read-only user and a read-only transaction in the code,
as the template does.

## Data copied into a cache

For data a credential can copy as files, when the data is too big to come back
through a stub, or more useful whole: objects in a cloud bucket, or a slice of a
large dataset that your group's own library knows how to find. A **fetcher** copies
the data into a cache. Scripts read the cache through a mount, with the whole of
DuckDB and no network.

The fetcher is the one part that holds a credential and reaches the network, so it
runs in a protected place:
- **A container of its own,** started for each fetch, from an image you build. The
  container sees nothing of your Mac.
- **A token narrowed to the job,** never your own credential. A credential helper on
  your Mac makes the token from your sign-in, able only to read one bucket, for an
  hour at most. Google Cloud Storage makes such tokens with a credential access
  boundary, and maji-mcp ships a helper for it:
  `maji-gcs-token --bucket <bucket> [--prefix <prefix>]`.
- **Only the hosts its connector file names,** such as `storage.googleapis.com`.
- **Only its own folder of the cache** to write.

**1. Write the fetcher.** A fetcher is code of your own: a connector file and an
image. The image reads one request as JSON on standard input, writes files under
`/cache`, and prints what it wrote. The connector file names the operations and their
parameters, the image, the hosts, the credential helper and the cache's limits:

```toml
namespace = "archive"
version = "0.1"
kind = "fetcher"
image = "archive-fetcher:0.1"        # you build it; approval pins its digest
hosts = ["storage.googleapis.com"]
token_from = ["maji-gcs-token", "--bucket", "example-weather-archive"]
cache = {size = "20GB", keep = "24h"}

[fetch.readings]
params = {station = "str", months = "list[str]"}
```

**2. Add it, and approve it,** as for [code of your own](#code-of-your-own). The item
shows the image's digest, the hosts, the credential helper's command, what the token
can read, and the cache's limits.

**3. Scripts fetch, then query:**

```python
import duckdb

got = archive.readings(station="KX12", months=["2026-09", "2026-10"])
db = duckdb.connect(got["path"], read_only=True)
print(db.sql("SELECT sensor, max(value) FROM readings GROUP BY sensor").fetchall())
```

The fetch returns the file's path under `/mnt/cache/archive/`, the data's version
and its size. A fetch of what is already in the cache returns at once. Executors whose
principal may call `archive` mount the cache read-only, so the script that fetched
reads the file in the same run.

**The fetcher saves files that hold their data.** A database whose views read from
the bucket fails in the sandbox, so the fetcher saves each table the request names as
a table.

**The data rests on your Mac's disk.** Unlike every other kind, a fetcher keeps the
service's data, in a Docker volume. The cache drops a file after `keep`, and the
oldest files beyond `size`. `maji-connectors purge archive` empties it at once.

**Objects copied as they are need no code.** A built-in fetcher copies a bucket's
objects by prefix ([connections.md](connections.md), logs from cloud storage). Write
a fetcher when the copy needs your code, such as a library that knows where each
station's readings are and how to correct them.

**While you try a fetcher out,** run the same code on your Mac as a command that
saves into a folder you mount ([Data in files on your Mac](#data-in-files-on-your-mac)).
The fetcher is that code moved into its protected place.

## Credentials

| Kind | The credential | Kept by |
|---|---|---|
| A REST API | a key you paste | the broker |
| An MCP server on the internet | an OAuth sign-in, or a key you paste | the broker |
| An MCP server on your Mac | none, or a key you paste | the broker |
| An MCP server you start as a command | whatever the program uses | your Mac |
| A database | a password you paste, or one a credential helper prints | the broker, or your Mac for each connection |
| Code of your own | whatever your code uses | your Mac |
| A fetcher | a token a credential helper narrows from your sign-in | your Mac makes it for each fetch; the fetcher's container holds it until the fetch ends |

A key you paste goes only to the broker, and only to the connector's own host
([../connections.md](../connections.md)).

## Letting an agent write a connector

maji-mcp ships a **Claude Code skill**, `maji-connector`, for when you would rather
describe the service than write the connector. Ask, for example, "connect maji to
Tripsy" or "let scripts query the shop's database". The Claude Code skill:

1. **Finds what the service offers:** an MCP server, REST API documentation or an
   OpenAPI description, or a database. The skill picks the kind in the order of
   [Choosing the kind](#choosing-the-kind).
2. **Writes the definition, or code of your own from the template,** with tests
   against a fake.
3. **Proposes the connector.** You approve the item, and paste a key if the connector
   needs one.
4. **Writes the first skills** against the live service with `run_python`, and keeps
   them. You promote the ones worth sharing.

The Claude Code skill follows the habits of the built-in connectors: a key never
appears in an error, page links are followed only on the same host, a redirect is an
error, every list has a cap and says so when it stops at the cap, failures carry
maji-mcp's error codes, and test fixtures are made up.

## Updating, removing and moving

- **A change that widens access raises an item** that shows the difference: a new
  tool, a changed classification, a new host, a new commit, a new command.
- **Removing:** choose **Remove** in the web app, run
  `maji-connectors remove <namespace>`, or send
  `DELETE /api/v0/connectors/<namespace>`. Removing takes effect at once, forgets the
  connector's key, and stops anything the launcher ran for the connector.
- **To another machine:** `maji-connectors export > connectors.json` writes every
  connector as JSON, without keys. `maji-connectors import connectors.json` on the
  other instance raises an item for each connector.

## Worked examples

### Tripsy, four ways in

Tripsy, a trip planner, offers every kind of MCP server and a REST API
([tripsy.app/claude](https://tripsy.app/claude),
[docs.api.tripsy.app](https://docs.api.tripsy.app/),
[github.com/tripsyapp/cli](https://github.com/tripsyapp/cli)):

| Way in | Kind | Credential |
|---|---|---|
| `https://mcp.tripsy.app` | an MCP server on the internet | OAuth sign-in |
| `tripsy-mcp --transport http --http-addr 127.0.0.1:8787` | an MCP server on your Mac | a Tripsy token you paste |
| `tripsy-mcp` | an MCP server you start as a command | the token `tripsy auth login` keeps in your Keychain |
| `https://api.tripsy.app` | a REST API | a Tripsy token you paste |

A good setup is the hosted server as `tripsy`, for the operations Tripsy chose and
described, and a REST definition as `tripsy_api` for reading in bulk. The hosted
server signs in with OAuth, which is the last step to build; until then, the local
HTTP server gives the same tools with a pasted token. No Tripsy token can be limited
to reading, so on every way in, maji-mcp's read-only rules are the only limit.

### A large dataset a group shares

Many groups keep a dataset that is too large to pass through an agent, and whose
meaning lives in the group's own code: which files hold what, what each column means,
which corrections apply. A research group's observations, a company's operational
records and a city's transit data are all of this kind. The data is best read with
Python, pandas or DuckDB, by a script that prints only the answer. maji-mcp aims to
make this kind of connector easy to add, and powerful for an agent to use.

The example is a made-up research group that keeps the readings of its weather
stations as Parquet files in a cloud bucket: one folder for each station and month,
a few hundred gigabytes in all. The group's own library knows the layout, applies
each sensor's corrections, and signs in with your cloud credentials.

The boundary is the library: it is the one part that needs your cloud credentials
and the network. Once the readings a question needs are copied out as a file,
reading them needs neither. The archive has two ways in:

1. **A query tool the group already runs.** If the group has a tool that answers
   queries over the archive and serves MCP, add the tool's address as an MCP server
   ([on your Mac](#an-mcp-server-on-your-mac) or
   [on the internet](#an-mcp-server-on-the-internet)).
   - The tool's query tools become reads. Any tool that saves something becomes a
     write, which the broker refuses.
   - Such a tool usually caps the rows a call returns, so a script counts and groups
     in the query, and pages when it needs every row.
   - Use it for a quick look, as the group's own code sees the data.
2. **A fetcher.** The group's library runs as a
   [fetcher](#data-copied-into-a-cache): in a container of its own, with a token
   that can only read the archive's bucket. The fetcher saves the stations and
   months a request names into one self-contained DuckDB file in the cache.
   - A script has the whole of DuckDB and pandas, with no row limit and no network.
   - Use it for heavy analysis: many queries, every row, or joins with other sources
     such as git history.
   - While you try it out, the same code runs on your Mac as a command that saves
     into a folder you mount.

Skills hold what the group knows about the data, such as which tables a question
needs and what their columns mean. Promoting a skill shares that knowledge with every
agent on the instance. Nothing about the group's dataset enters maji-mcp's
repository: the tool's address is in your instance, the fetcher is in the group's
repository, and the skills are in your library.

## Decisions this design makes

Each becomes an entry in [../../decisions.md](../../decisions.md) once the design is
agreed.

- **A key's host can come from a definition you approve.** Today a key's host comes
  only from the broker's configuration, never from a form
  ([../connections.md](../connections.md)). A REST or database definition names its
  own host, so the host is something you paste, and approving it takes your passkey
  as a high-risk item.
- **A client token may propose a connector.** A client token already proposes
  Connect items (`POST /connections/{service}/requests`). A connector is the first
  high-risk item a client token can propose, and the token still decides nothing.
- **Connectors run outside the broker.** The broker holds every key, so no code of
  your own runs in the broker. Glue you write runs in the sandbox as skills, or on
  your Mac as code of your own.
- **The launcher is the only way a decision runs a command on your Mac.** Today no
  container can make your Mac run anything. The launcher runs only commands you
  approved, so the control plane's records now decide what runs on your Mac. The
  launcher checks the signature on each approval itself.
- **A fetcher gets a token narrowed to its job, never your credential.** A cloud
  sign-in can do whatever you can. A fetcher needs to read one bucket for a few
  minutes, so a credential helper on your Mac narrows the token, and the fetcher's
  container reaches only the hosts its connector file names.
- **A fetcher's data rests on your disk, with limits.** Every other kind leaves the
  service's data in the service. A fetcher's cache has a size and an age limit, and
  can be emptied at once.

## Open questions

- **What becomes of `config/servers.json`, `make bridge` and the emacs-org adapter.**
  emacs-org-mcp fits an MCP server you start as a command, but its records come from
  an adapter in maji-mcp's repository, which a connector can't carry. Either the
  adapter stays built in for `org`, or it becomes skills.
- **How an agent without a shell proposes.** Claude Desktop can't run
  `maji-connectors`. A gateway tool that proposes a connector would let any agent
  propose one.
- **Scoping a database to one schema at a time.** A query stub that takes the schema
  as a parameter could be granted per schema for a session, as paths are. But a user
  that can read every schema lets SQL reach any of them anyway, so real scoping needs
  a database user for each schema.
- **Narrowing a token made from your own sign-in.** Google's credential access
  boundaries are documented with service accounts. That narrowing works from your
  user credential needs checking.
- **How a fetcher's hosts are enforced:** an egress proxy that allows only the named
  hosts, and whether every library the fetcher uses honours the proxy.
- **What a fetch of the newest data checks.** The cache is keyed by version, so a
  fetch without a version asks the service which version is newest first.
- **What a REST definition may limit:** path prefixes only, or also parameters.
- **Connectors in maji-mcp's own containers,** for code of your own that needs only a
  pasted key, so the code does not run as you.
- **The page's name:** connectors on the Connections page, or a page of their own.

## Building it

Data in files on your Mac needs nothing built. The rest, in order:

1. MCP servers on your Mac and on the internet, with a key or none: the item, the
   classification, the `Host` the broker sends, and the broker adding a namespace
   without a restart. This step reaches a group's dataset through a tool the group
   already runs.
2. REST definitions and their stubs, and the `maji-connector` Claude Code skill.
3. The launcher: MCP servers you start as commands, code of your own, tunnels and
   credential helpers.
4. Fetchers and the cache: the fetcher's container, its hosts, `maji-gcs-token`, and
   the built-in fetcher for a bucket's objects. This step brings heavy analysis of a
   group's dataset.
5. Database definitions.
6. OAuth sign-in for MCP servers on the internet ([connections.md](connections.md)).
