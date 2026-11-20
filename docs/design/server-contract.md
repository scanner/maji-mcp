# The server contract: what the broker follows today

What the broker and the bridge do for an MCP server that scripts reach through
maji-mcp. The server contract is the interface between such a server and maji-mcp.
This document holds the clauses of the contract that are built.

**Status:** built. The contract is at version **0.1**, and stays at 0.x while the
design is in flux. The clauses that are not built, and the runtime host profile, are
in [planned/server-contract.md](planned/server-contract.md). A clause keeps one
number across both documents, so "B4" names the same clause wherever a document or a
module cites it. No server implements the whole contract yet: emacs-org-mcp is used
as it is, through the bridge ([broker.md](broker.md)).

| Clause | Built | Planned |
|---|---|---|
| A1, structured output | | all of it |
| A2, structured errors | the codes, and the broker passing a tool's code on | |
| A3, reads report versions | | all of it |
| A4, writes take preconditions | | all of it |
| A5, a timed-out write has an unknown outcome | | all of it |
| A6, the broker decides whether a tool reads or writes | a classification written in `config/servers.json` | a classification proposed from the server's hints, and approved in the queue |
| A7, versioned schemas | a version for each server in `config/servers.json` | a server declaring its own version |
| A8, caller identity in `_meta` | all of it | |
| B1, who Layer B covers | servers that act as you, started by you | servers the broker launches in a container |
| B2, transport | HTTP on `127.0.0.1` with a token, through the bridge | a Unix socket on a shared kernel |
| B3, socket location and access | | all of it |
| B4, registration | the bridge writes the registration file | the broker reading it |
| B5, approval | | all of it |
| B6, lifecycle | | all of it |
| B7, limits of Layer B | data sent to a server that acts as you | what a runtime host runs |
| The runtime host profile | | all of it |

There are two layers:
- **Layer A** applies to every server behind the broker.
- **Layer B** applies to servers that act as you, and covers how they are reached.

## Layer A: every server

**A2. Structured errors.** A failed call returns `isError: true`, with
`structuredContent` of `{code, message, data}`. The codes are:

| Code | Meaning |
|---|---|
| `conflict` | a write's precondition no longer holds; `data` carries the current version |
| `not_found` | the target doesn't exist |
| `invalid_argument` | the arguments don't fit the schema or the target |
| `unavailable` | the server can't serve the call right now |
| `internal` | anything else |

- **The broker passes a tool's code on to the script** when a failed call carries one
  of the codes, and reports `internal` otherwise
  (`src/maji_mcp/broker/mcp_servers.py`).
- **A server the broker cannot reach is `unavailable`,** and so is a server that
  answers with an HTTP error, such as a refused token.
- **Every component of maji-mcp uses the same codes** between themselves
  (`src/maji_mcp/common/protocol.py`). The broker adds three for its own refusals:
  `unauthenticated`, `permission_denied` and `rate_limited`.

**A6. The broker decides whether a tool reads or writes.** `config/servers.json`
lists each tool the broker exposes, as a read or a write.
- A tool the server offers and the file does not list is never exposed, and its
  description never reaches `read_api`.
- A tool listed as a write becomes a stub the broker refuses, until changesets are
  built ([planned/writes.md](planned/writes.md)).

**A7. Versioned schemas.** The servers file gives each server a version, which
becomes the namespace's version.

**A8. Caller identity in `_meta`, for logs only.** Every request carries
`maji-mcp/principal` and `maji-mcp/run_id` in its `_meta`. The server can record them
in its own logs. Authorization stays with the broker, and the server never decides
access from these fields.

## Layer B: servers that act as you

**B1. Who this covers.** Servers that act on your files, your editor or your local
apps, such as emacs-org-mcp. They run under your user and are started by you, never by
the broker. `make bridge` starts emacs-org-mcp that way.

**B2. Transport.** Across a VM boundary, the transport is streamable HTTP on
`127.0.0.1`, with a bearer token in the `Authorization` header, read from a token
file.
- **This is the normal case on a Mac.** The broker runs in a container on the
  container VM's Linux kernel, and a server that acts as you runs on macOS. A
  container can't connect to a Unix socket served on the Mac, even when the socket
  file is bind-mounted into it; that test was refused. A service on the Mac's
  `127.0.0.1` can be reached from a container at `host.docker.internal` (see
  [deployment.md](deployment.md)). So every server that acts as you on a Mac offers
  HTTP.
- **The token file is readable only by you.** The bridge creates the file that way,
  narrows the mode of an existing file that others could read, and does not start on
  a file that holds no token (`src/maji_mcp/host/bridge.py`).
- **Over HTTP, the token is the only gate.**

**B4. Registration.** A server id is 1 to 64 letters, digits, hyphens and underscores,
and starts with a letter or a digit, because the id names the server's files. While
the bridge runs, it keeps `<server-id>.json` in `~/.config/maji-mcp/servers/`, and
removes the file on a clean stop:

```json
{
  "id": "emacs-org",
  "name": "emacs-org",
  "contract": "0.1",
  "transport": "http",
  "url": "http://127.0.0.1:8766/mcp",
  "token_file": "/home/you/.config/maji-mcp/secrets/emacs-org.token",
  "concurrency": 1
}
```

- `concurrency` is how many calls the server takes at once. The bridge passes calls
  through one at a time.
- The broker does not read the file. The broker's servers come from
  `config/servers.json`, and the broker connects to each as the broker starts.
- A killed bridge leaves the file behind.

**B7. Limits of this layer.** Anything that runs as you can stop a server that acts as
you and take its place. Data the broker sends to such a server is therefore visible
to any process running as you. That gives nothing beyond what your user already has,
so maji-mcp still only adds a way to work. The contract promises no more than that.

## Planned

[planned/server-contract.md](planned/server-contract.md) holds the rest of the
contract:

- **Structured output, versions and preconditions** (A1, A3, A4, A5), which writes
  need ([planned/writes.md](planned/writes.md)).
- **A classification proposed from the server's hints,** and a version the server
  declares (A6, A7).
- **A Unix-socket transport** for a host where the broker and the server share a
  kernel (B2, B3).
- **Registration the broker reads, approved in the queue,** and reconnecting to a
  server that was down (B4, B5, B6).
- **The runtime host profile,** for a server that hosts a skill runtime such as
  `elisp-live`.
