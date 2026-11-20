# The gateway

The gateway is the MCP server that apps connect to. This document covers the gateway's
tools, how the gateway identifies a principal and a session, and the two programs that
connect apps to the gateway: the headers helper and the relay.

**Status:** this document describes what is built. Code: `src/maji_mcp/gateway/server.py`,
`src/maji_mcp/host/relay.py`, `bin/maji-headers`.

## What the gateway does

The gateway serves MCP over streamable HTTP, published on `127.0.0.1:8765` only. The
gateway does three things for each request:

1. finds the principal from the gateway token
2. finds the session
3. passes the work on: a script to the runner, everything else to the broker

The gateway holds no credentials for services and runs no agent code. The gateway
accepts requests only for the hosts `127.0.0.1`, `localhost` and `[::1]`, as a guard
against DNS rebinding.

## Tools

| Tool | What the tool does |
|---|---|
| `run_python(code, timeout_s=60, paths=None, why=None)` | Runs a script in an executor and returns what the script prints. |
| `list_api()` | Lists the namespaces, stubs and skills the principal may call. |
| `read_api(name)` | Shows the Python signature and documentation of a stub or a namespace. For a skill, `read_api` shows the skill's code too. |
| `search_api(query)` | Finds stubs and skills whose name, summary or parameters mention every word of the query. |
| `save_skill(name, summary, run=None, code=None)` | Keeps a function from a script as a skill ([skills.md](skills.md)). |
| `promote_skill(name, why=None)` | Asks you to promote the version of a skill the principal kept last ([skills.md](skills.md)). |

The broker answers `list_api`, `read_api` and `search_api`, so an agent sees only the
stubs the policy allows the agent's principal.

### `run_python`

- **The script runs as the token's principal.** Each namespace the principal may call
  is a global in the script, such as `sentry`.
- **`timeout_s`** is 60 seconds unless the agent asks for more, up to 600.
- **The output is capped at 64 KB.** Past the cap, the result says the output was cut.
- **`paths`** names up to 10 folders or files under your home directory for the
  script to read. `why` says why the script needs the paths; you read `why` on the
  item, labelled as the agent's words. [runner.md](runner.md) covers mounts.
- **The result is the script's output, then one status line:**

  ```
  [run 1a2b3c4d: exit 0, mounted /mnt/api, 812 ms; called sentry.issues ×3, skills.culprits]
  ```

  The status line gives the first 8 characters of the run id, the exit code or
  "timed out", the mounts, the duration, and the stubs and skills the run called. The
  list of calls comes from the broker's run log, so the list shows what the script
  did, whatever the script's code looked like. Standard error is included only when
  the run failed.
- **A mount that left files out says so.** A line before the status line names the
  files that read as empty, such as `.env`, so the agent does not take an empty read
  for an empty file ([runner.md](runner.md)).
- **A run that waits on a grant does not start.** The result names the paths, the
  item, and the session name the queue shows, so the agent can tell you which item is
  the agent's own. The agent runs the same request again once you have decided.

### Errors

A component that refuses a request answers with a code and a sentence. The gateway
passes both to the agent as a tool error, so the agent learns why a request was
refused and what to do instead. The codes are `not_found`, `invalid_argument`,
`conflict`, `unavailable`, `internal`, `unauthenticated`, `permission_denied` and
`rate_limited`.

## Principals and tokens

Each principal has one gateway token. `make setup` creates a token for the principal
`claude-code`, writes the token to `claude-code.token` in the secrets directory, and
writes the token's SHA-256 hash to `gateway_tokens.json` there. The gateway reads the hashes when the
gateway starts and compares hashes in constant time.

The token decides the principal. Nothing an agent sends can change the principal. Two
apps that hold the same token are the same principal: the two apps share policy, rate
limits, grants that last beyond a session, and kept skills. An app that needs different
treatment needs its own token, and so its own principal (`make token PRINCIPAL=name`,
plus an entry in the policy file).

An app also reports a name and a version when the app connects, such as
`claude-code 2.1.322`. The gateway passes the name and version along, and an item
shows them after the principal. Nothing checks what an app reports.

## Sessions

A session is what a grant "for this session" lasts for, and what saved values belong
to. The gateway derives a session key for each request, in one of two ways.

| The app speaks | The session is | The session ends when |
|---|---|---|
| an MCP revision with sessions (before 2026-07-28) | the MCP session the gateway issued | the app ends the MCP session, the MCP session goes an hour with no call in flight and no stream open, or the gateway restarts |
| the 2026-07-28 revision, which has no sessions | the id the app names in the `X-Maji-Session` header | the app sends a different id, the session goes an hour without a script, or the gateway restarts |
| neither | no session | |

- **The session key is a SHA-256 of the principal and the session's id.** For a named
  session the hash also covers a nonce the gateway draws when the session begins. An
  id presented with another principal's token therefore reaches a different session.
- **A named id is 16 to 128 letters, digits, `-` or `_`.** The id is the app's own. The
  id tells one session of an app from another session of the same app. The id never
  tells one principal from another: the gateway token does that.
- **A request with no session still runs.** Saved values need a session. A mount for a
  request with no session is granted for a time, and a refused path stays refused
  until you let the agent ask again.
- **A session has a session name.** `session_name` in `src/maji_mcp/common/names.py`
  picks three words by a hash of the session key: a present participle, an adjective
  and a noun, such as `laughing-sparky-daisy`. Every component computes the same
  session name from the same session key, so no component keeps a table of names. No
  two words in a list are within one letter of each other. The word lists never
  change, because a change would rename every session.
- **The gateway logs each session's life:** the start, the principal, the app, and
  the end. `make logs SERVICE=gateway` shows the log. The log shows a session id by
  the id's first 8 characters.

The gateway keeps the last 20 successful scripts of each session in memory, 32 MB in
all, so `save_skill` can name a run. A restarted gateway keeps none, and the agent
then sends the code.

## How Claude Code connects

`make connect-claude-code` adds the gateway to Claude Code as an HTTP MCP server named `maji`,
with a `headersHelper`. Claude Code runs the headers helper, `bin/maji-headers`, each
time Claude Code connects: at the start of a Claude Code session, and on reconnect.
The headers helper prints two headers:

- `Authorization`, with the gateway token read from `<principal>.token` in the
  secrets directory
- `X-Maji-Session`, with 32 new random hex digits

The gateway token is therefore never copied into Claude Code's settings, and each
connection of Claude Code is one session. A session outlasts `/clear` and compaction
in Claude Code, because neither reconnects.

## How Claude Desktop connects: the relay

Claude Desktop starts each local MCP server as a command and speaks MCP over the
command's standard input and output. Claude Desktop's connectors for remote servers
connect from Anthropic's cloud, which cannot reach `127.0.0.1`. `make connect-claude-desktop`
therefore adds the relay, `maji-relay`, as the command.

- **The relay serves the gateway's instructions and tools over stdio,** and passes
  each call to the gateway over HTTP.
- **The relay reads the gateway token from the token file for every request.** The
  token never sits in Claude Desktop's settings, and a token replaced by
  `make reconnect-claude-code` is used from the next request on.
- **The relay sends requests only to a loopback address.** The relay refuses any
  other URL, so the token stays on your machine.
- **The relay speaks the 2026-07-28 revision, with a new MCP client for each
  request.** A gateway restart fails only the calls made while the gateway is down.
- **One relay is one session.** The relay names one session id for as long as the
  relay runs. Claude Desktop keeps one relay for as long as Claude Desktop runs, so
  every chat shares one session: a grant for the session holds for every chat, until
  Claude Desktop quits or the session goes an hour without a script.
- **A relay that cannot reach the gateway at start exits with a message.** Claude
  Desktop then shows `maji` as failed, and
  `~/Library/Logs/Claude/mcp-server-maji.log` holds the message.

## What the gateway tells agents

The gateway's MCP `instructions` reach an agent when the agent's app connects. Claude
Code puts the instructions into the system prompt. The instructions say when a script
beats a series of tool calls, give the cost estimate, and introduce stubs, saved
values, mounts and skills. [agent-guidance.md](agent-guidance.md) covers the
reasoning. Each tool's description carries the detail an agent needs at the moment
of the call.

## Planned

- **Tools for an agent's own queue items:** `submit` for a question, `status` and
  `withdraw` ([planned/queue.md](planned/queue.md)). The control plane already serves
  the socket these tools will use.
- **Skills as tools of their own,** found by search, beside `skills.<name>` in a
  script ([../roadmap.md](../roadmap.md)).
- **TLS or a private network in front of the gateway,** for an instance on a remote
  host ([deployment.md](deployment.md)).
