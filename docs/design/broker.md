# The broker

The broker is the only component that holds credentials and calls services. This
document covers stubs, the checks on each stub call, the services the broker reaches
today, saved values and the run log.

**Status:** this document describes what is built. Code: `src/maji_mcp/broker/`,
`src/maji_mcp/host/bridge.py`, `config/`.

## What the broker does

The broker runs no agent code. The broker serves two Unix sockets, each on a volume of
its own, so the containers that mount a volume decide who can reach a socket.

- **The call socket** is shared with every executor, read-only. A script's stub calls
  arrive on the call socket, each with the run token.
- **The control socket** is shared with the runner and the gateway only. The runner
  registers and finishes runs on the control socket. The gateway reads the API
  documentation for a principal, and keeps and promotes skills.

The broker is also the control plane's client. The broker raises items, asks whether a
grant covers a request, and reads your decisions from the decision feed
([control-plane.md](control-plane.md)).

## Stubs and the catalog

A stub is a function a script calls and the broker carries out. A stub is named
`<namespace>.<operation>`, such as `sentry.issues`. The catalog
(`src/maji_mcp/broker/catalog.py`) holds every stub with:

- **parameters,** each with a type and a description
- **documentation:** a summary, what the stub returns, and notes
- **a classification:** the stub reads, or the stub writes
- **a handler:** the code that calls the service

The catalog checks a call's arguments against the stub's parameters before the handler
runs. A misspelt or missing argument comes back as `invalid_argument`, and never as a
confusing failure from the service or an empty result.

The same definitions render the documentation an agent reads. `read_api` shows each
stub as a Python signature with a docstring.

**Stub names are stable, and each namespace has a version.** A breaking change to a
stub's signature is a new major version of the stub's namespace. A skill written
against `sentry` 1.x keeps working until a breaking change makes `sentry` 2.0.

## The checks on every stub call

For each call on the call socket, the broker:

1. finds the run from the run token, and so the principal; an unknown token is
   `unauthenticated`
2. checks the policy: the principal must be allowed the stub, or the call is
   `permission_denied`
3. refuses a stub classified as a write, with `permission_denied`
4. takes one unit from the rate limit for the principal and the namespace, or the
   call is `rate_limited`
5. checks the arguments against the stub's parameters
6. calls the handler, with a 120-second limit
7. writes one row to the run log: the stub, the outcome, the duration and the size
   of the result

The broker works on at most 8 of one run's calls at once. One message is at most
16 MB.

### Policy

The policy is a JSON file, `config/policy.json`, mounted into the broker:

```json
{
  "principals": {
    "claude-code": {"allow": ["maji.*", "sentry.*", "jira.*", "github.*", "org.*", "skills.*"], "per_minute": 120}
  }
}
```

- **`allow`** holds shell-style patterns over stub names.
- **A principal the file does not name may call nothing.**
- **`per_minute`** is the rate limit. The limit is a token bucket for each principal
  and namespace, so one busy script cannot use up a service's allowance for every
  other caller.
- **The broker reads the file when the broker starts.** After an edit, run
  `make restart SERVICE=broker`.

### Reads and writes

Whether a stub reads or writes is decided once, when the stub's service is set up. The
broker never infers the classification from a script's code. The broker checks every
call as the call is made, so the run log records what a run reached, whatever the
run's code looked like.

The broker refuses every write stub today. Writes arrive with changesets
([planned/writes.md](planned/writes.md)).

### Errors

A refused or failed call raises `maji.StubError` in the script, with a code:

| Code | Meaning |
|---|---|
| `unauthenticated` | the run token is unknown, or the run has ended |
| `permission_denied` | the policy does not allow the call, or the stub writes |
| `rate_limited` | the principal's allowance for the namespace is used up for now |
| `invalid_argument` | the arguments do not fit the stub's parameters |
| `not_found` | the target does not exist. For a stub's name, the error lists the stubs of that namespace the principal may call, and no others. |
| `conflict` | something changed since the caller read it |
| `unavailable` | the service cannot be reached, or refused the broker's credential |
| `internal` | anything else |

## The services the broker reaches

### Built in: the `maji` namespace

| Stub | What the stub does |
|---|---|
| `maji.whoami` | The principal the run acts as, and the run id. |
| `maji.save`, `maji.load`, `maji.saved` | Saved values for the session. |

### Sentry: the `sentry` namespace

`sentry.projects`, `sentry.environments`, `sentry.issues`, `sentry.events` and
`sentry.tag_values` call Sentry's REST API, read-only
(`src/maji_mcp/broker/sentry.py`).

- **The REST API, not Sentry's MCP server.** A script needs every issue and its
  counts in the environments it asks about. Sentry's MCP server returns 100 issues at
  most, counts every environment whatever the filter, and runs a model on every
  search ([connections.md](connections.md)).
- **The credential is a read-only user auth token you paste into the web app,** with
  your organization's slug. The broker keeps the token in its credential store, sends
  the token only to the configured Sentry host (`SENTRY_URL`), and reads it for each
  request. The token never appears in a result, an error or the run log.
- **The stubs are there before Sentry is connected.** The first call asks you to
  connect Sentry, and fails at once naming the Connect item
  ([connections.md](connections.md)). So are Jira's and GitHub's.

### Jira: the `jira` namespace

`jira.projects`, `jira.fields`, `jira.issues`, `jira.count`, `jira.issue` and
`jira.changelog` call Jira Cloud's REST API, read-only (`src/maji_mcp/broker/jira.py`).

- **Records, not Jira's objects.** An issue comes back with names in place of nested
  objects, and rich text as plain text. A script names any other field by its id, and
  `jira.fields` finds a custom field's id by its name.
- **A search is read to its end,** page by page, up to the call's `limit`.
  `jira.count` says how many issues a query matches without reading them.
- **The credential is a scoped Atlassian API token,** given with your site's name and
  your email, and sent only to Atlassian's API gateway ([connections.md](connections.md)).

### GitHub: the `github` namespace

`github.repos`, `github.pulls`, `github.pull`, `github.commits` and `github.search`
call GitHub's REST API, read-only (`src/maji_mcp/broker/github.py`).

- **Records, not GitHub's objects:** logins in place of users and labels' names in
  place of labels. `github.pull` adds a pull request's reviews and the files it changed.
- **A list is read up to the call's `limit`.** A search also says how many issues and
  pull requests match. GitHub gives at most 1,000 of a search.
- **The credential is a fine-grained personal access token** that can only read, sent
  only to `GITHUB_API_URL` ([connections.md](connections.md)).

### MCP servers behind the broker

The broker calls MCP servers as a client. `config/servers.json` lists each server:

```json
{
  "servers": {
    "org": {
      "url": "http://host.docker.internal:8766/mcp",
      "token_file": "/run/secrets/emacs_org_token",
      "version": "1.0",
      "adapter": "emacs-org",
      "tools": {"get_task": "read", "list_tasks": "read"}
    }
  }
}
```

- **The key is the namespace.** The tool `get_task` of the server `org` becomes the
  stub `org.get_task`.
- **The file decides which tools become stubs, and whether each reads or writes.** A
  tool the server offers and the file does not list is never exposed, and the tool's
  description never reaches `read_api`. A server that adds a tool changes nothing
  until you list the tool.
- **Every call carries the caller in `_meta`:** `maji-mcp/principal` and
  `maji-mcp/run_id`, from the broker's own record of the run. The server can log the
  caller. The server never decides access from `_meta`.
- **The broker connects to each server when the broker starts.** A server that cannot
  be reached at start is reported in the broker's log and left out. `make bridge`
  therefore restarts the broker once the bridge answers.
- **Existing servers are used as they are.** maji-mcp adapts to a server and asks for
  no change to it. [server-contract.md](server-contract.md) holds what the broker
  relies on today, and [planned/server-contract.md](planned/server-contract.md) what a
  server will provide so the broker can use it fully.

### The bridge

A server that acts as you, on your files, runs on your machine and speaks MCP over
stdio. The broker runs in a container on the container VM's kernel. A container
cannot reach a Unix socket or a process on the Mac, and can reach the Mac's
`127.0.0.1` through `host.docker.internal`. The bridge (`maji-bridge`) closes the gap
without changing the server:

- The bridge starts the server as a child process, as you, and keeps one MCP session
  open with the server.
- The bridge serves the same tools over streamable HTTP on `127.0.0.1` only, behind a
  bearer token read from a file only you can read.
- The bridge passes each call through unchanged, one call at a time, with the
  caller's `_meta`.
- The bridge writes a registration file while the bridge runs
  ([server-contract.md](server-contract.md), B4), and removes the file when the
  bridge is stopped. A killed bridge leaves the file behind.

`make bridge` starts the bridge in front of emacs-org-mcp, in the background, and
`make bridge-stop` stops it. The bridge does not decide what the broker may call:
`config/servers.json` does.

### emacs-org: the `org` namespace

emacs-org-mcp answers in text written for a model to read. A script needs fields. For
a server configured with `"adapter": "emacs-org"`, the broker adds two stubs that call
the server's own read tools and parse the text (`src/maji_mcp/broker/emacs_org.py`):

- `org.tasks(section, where)` returns tasks as records: drawer properties, ISO
  timestamps, checklist counts and open items.
- `org.task(id)` returns one task as a record, with the task's exact org text.

The adapter adds the two stubs only when the file lists `list_tasks` and `get_task`
as reads. The server's other listed read tools are stubs too, and return the
server's text. Calls to the server run one at a time, at the bridge.

## Saved values

Each script starts in a fresh executor. `maji.save(name, value)` keeps a value for the
session's later scripts, and `maji.load(name)` returns the value, so a later script
need not fetch the same data again.

- **The broker holds saved values in memory,** as JSON text. A loaded value is parsed,
  never evaluated.
- **A saved value belongs to the session the run was registered under.** A script
  cannot name another session.
- **Bounds:** 8 MB for one value, 32 MB for one session, 256 MB for all sessions. Past
  the total, the broker drops the sessions used longest ago. The broker drops a
  session unused for an hour.
- **A session can outlast one task.** A name saved for an earlier task is still
  there, and `maji.saved()` shows when each value was saved.
- **A restarted broker has no saved values.**

## The run log

The run log is a SQLite database on a volume of its own
(`src/maji_mcp/broker/runlog.py`).

- **One row for each run:** the run id, the principal, the runtime, the SHA-256 of
  the script, the start, the duration, the exit code, whether the run timed out, the
  size of the output, and whether the run reported the runtime libraries it used.
- **One row for each stub call:** the stub, the outcome, the duration and the size of
  the result.
- **One row for each runtime library a run used:** whether the run imported it, how
  often the script called `help()` on it, the class of the exception a failed run
  ended with, and whether the library raised it. A class is kept by name when Python,
  the stub client or a runtime library defines it; a class the script defined, which
  can carry a project's names, is recorded as `other`. The executor reports these as
  the script ends, on the call socket with the run's token. The broker takes the
  first report of a run only, keeps only the names of runtime libraries, and doesn't
  count the report as a call. The broker marks the run as having reported, whether or
  not it used a runtime library. A run stopped at its time or memory limit sends no
  report.
- **One row for each read of the runtime libraries' notes** through `read_api`, by
  principal.
- **Never the arguments or the results,** which can hold your data.

The run log has three uses today. `run_python`'s result names what a run called, from
the run log. `save_skill` checks a named run against the run log: the same principal,
the same script, and exit code 0. `make library-use` counts how scripts used the
runtime libraries ([agent-guidance.md](agent-guidance.md)).

## Paths to mount

A run that names paths starts only when grants cover every path
(`src/maji_mcp/broker/mounts.py`). For each path the runner resolved, the broker asks
the control plane whether an active grant covers the principal mounting the path in
the session. For a file named like a secret, the broker asks for a grant on exactly
that path, because a grant on a folder never covered what the folder's mount leaves
out. For the paths no grant covers, the broker raises one item, with a fact that says
what a mount of each path leaves out, and remembers the item for the principal, the
session and the path:

- **While the item waits,** a rerun is told about the same item.
- **Once you have refused a path,** a rerun in the same session is refused, until you
  let the agent ask again.
- **Once the item has lapsed or been withdrawn,** or the grant has lapsed or been
  revoked, the path is asked for again.

The broker holds this record in memory. After the broker restarts, a waiting or
refused path can be asked for once more.

## Planned

- **Write stubs record changesets** ([planned/writes.md](planned/writes.md)).
- **Connections:** OAuth sign-in to remote MCP servers, Sentry's MCP server as a
  second namespace, and Atlassian's MCP server checked
  ([planned/connections.md](planned/connections.md)). Keys for services that take an
  API key are built ([connections.md](connections.md)).
- **Server registration through the queue,** replacing `config/servers.json`: the
  broker watches the registration directory, pins each server's tool schemas, and
  reconnects with backoff
  ([planned/server-contract.md](planned/server-contract.md), B5 and B6).
- **Grants from the control plane in place of the policy file.**
- **Generated typed stubs.** A script calls stubs through a dynamic client today.
  Typed stubs would turn a wrong field name into an error before the call is made.
- **The run log as a source of proposals:** measured savings for each skill, and
  scripts worth keeping ([planned/agent-guidance.md](planned/agent-guidance.md)).
