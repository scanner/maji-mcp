# Planned: the rest of the server contract

What an MCP server will provide so the broker can use the server fully: structured
output, versions and preconditions for writes, a Unix-socket transport, registration
approved in the queue, and a profile for servers that host a skill runtime.

**Status:** planned. Nothing in this document is built. The clauses the broker and
the bridge follow today are in [../server-contract.md](../server-contract.md), and a
clause keeps one number across both documents. The contract is at version **0.1**,
and stays at 0.x while the design is in flux. The clauses below are written as the
contract will read, so they are in the present tense.

Servers that implement the contract, such as the planned Emacs MCP server
([emacs-mcp.md](emacs-mcp.md)), live in their own projects. The contract is the
interface between those servers and maji-mcp.

There are two layers and one profile:
- **Layer A** applies to every server behind the broker.
- **Layer B** applies to servers that act as you, and covers how they are reached.
- **The runtime host profile** applies to servers that host a skill runtime, such as
  `elisp-live`.

## Layer A: every server

Your own servers implement all of Layer A. A third-party server that doesn't is wrapped
by the broker: its stubs parse what they can, and its writes are marked unguarded in
the queue. For third-party services, a REST API is usually the better source (see
[../../research/evaluation.md](../../research/evaluation.md)).

**A1. Structured output.** Every tool declares an `inputSchema` and an `outputSchema`,
and returns `structuredContent`. It can return text content alongside, for other MCP
clients.

**A3. Reads report versions.** Every object a read tool returns carries a `version`. A
version is an opaque token, such as a content hash or a revision, that changes whenever
the object changes.

**A4. Writes take preconditions.** A write tool accepts `if_match`: the version of each
object it changes, as the caller last read it.
- On a mismatch, the tool changes nothing and returns `conflict`.
- When the broker records a changeset, the precondition is the version the script
  read, not a fresh read taken at record time. A fresh read would hide a change made
  between the script reading the object and the broker recording the write.
- A write tool that can't support `if_match` can still be approved, but its writes are
  marked unguarded in the queue.

**A5. A timed-out write has an unknown outcome.** The broker can stop waiting, but the
server may still apply the write. A single-threaded server such as Emacs often will.
- The broker never retries blind. A retry carries the same `if_match`, so a write that
  already landed comes back as `conflict`.
- Only tools declared idempotent are retried without a precondition.

**A6. The broker proposes a classification from the server's hints.** MCP's tool
annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`) are hints from the
server. When you approve a server, the broker proposes a classification from them, you
confirm it, and it is pinned along with the schemas. Today the classification is
written by hand in `config/servers.json`.

**A7. A server declares its own version.** Tool names are stable, and a breaking
change to a tool's schema is a new major version. Today the version comes from
`config/servers.json`.

## Layer B: servers that act as you

**B1. Servers that only call remote services.** They run in maji-mcp's containers,
launched by the broker over stdio, and Layer B doesn't apply to them.
[connectors.md](connectors.md) designs how such a server is added and approved.

**B2. A Unix socket on a shared kernel.** Where the broker and the server run on the
same kernel, as on a Linux host where they run side by side, a Unix socket also works,
carrying MCP's stdio framing: newline-delimited JSON-RPC.
- It isn't one of the transports the MCP specification defines. It is the stdio
  message format on a socket instead of a pipe.
- The broker passes the socket's messages into the SDK's `ClientSession`, which takes
  message streams rather than a process.

A server may offer both transports. Over HTTP, the token is the only gate, so B3
applies only to sockets.

**B3. Socket location and access.** This clause applies to the Unix socket transport
only. Sockets live in one runtime directory per user.
- **The directory is the access gate.** It is owned by you, the service user's group
  can traverse it, and no one else can. The socket file's own mode isn't relied on.
  Platforms differ in whether they enforce it, and Emacs creates sockets with your
  umask (a test gave `srwxr-xr-x`).
- **The broker checks the peer's uid where the OS supports it,** to confirm the socket
  is served by your user.
- **Paths are short.** Unix socket paths are limited to about 100 bytes. A 117-byte path
  failed in Emacs with "Service name too long". So the runtime directory is set at
  install to a short path.
- **Exact modes, group setup and peer checks are verified per platform at build
  time.**
- **Stale sockets.** A server that finds its socket path already exists tries to
  connect to it. If the connection is refused, the socket is left over from a crash, and
  the server replaces it. If a live server answers, the new one exits, so there is one
  server per id.

**B4. The broker reads registrations.** A server on a socket writes
`<server-id>.json` beside its socket at start, and removes it on a clean stop:

```json
{
  "id": "emacs",
  "name": "Emacs MCP server",
  "version": "0.3.0",
  "contract": "0.1",
  "transport": "socket",
  "socket": "emacs.sock",
  "concurrency": 1,
  "runtimes": ["elisp-live"]
}
```

`concurrency` is how many calls the server takes at once, and the broker serializes
calls to match. Today the bridge writes a registration file and the broker does not
read it. [connectors.md](connectors.md) designs the broker reading the registrations
of connectors' bridges.

**B5. Approval.** Anything running as you can write a registration file. A server's
tool descriptions then become `read_api` documentation for every principal, including
remote ones. So:
- **The first registration of a server id is a queue item.** It shows the tools, their
  schemas, and the proposed read/write classification.
- **Approval pins the id, a hash of the tool schemas, and the classification.**
- **If the schemas have changed when the server reconnects,** the change is a queue item
  showing the diff. Unchanged tools keep working. New or changed tools aren't offered
  as stubs until you accept them.

**B6. Lifecycle.**
- The broker watches the runtime directory. When a registration appears, it connects,
  initializes, and checks the contract's major version, refusing a server on an
  unknown one.
- When a server is down, its tools and runtimes are reported as unavailable on that
  instance, and the broker reconnects with backoff.

**B7. Limits for a runtime host.** The broker's signature check governs what the
broker *sends* to a runtime host. It can't govern what the host then runs. The limit
that already holds, on data sent to a server that acts as you, is in
[../server-contract.md](../server-contract.md).

## Runtime host profile

A server that hosts a skill runtime lists it under `runtimes` in its registration and
implements these tools. `elisp-live` is the first. The profile is generic, so another
editor or application could host a runtime the same way.

| Tool | Arguments | Returns |
|---|---|---|
| `load_skill` | `name`, `runtime`, `source`, `content_hash` | `{name, content_hash}` |
| `call_skill` | `name`, `content_hash`, `arguments` | the skill's output, matching the output schema in its manifest |
| `list_skills` | none | `[{name, content_hash}]` |
| `unload_skill` | `name` | `{name}` |
| `eval` | `source` | the value, as JSON |

The profile adds two error codes to A2's: `not_loaded` and `stale`.

- **The broker loads a skill only after checking its signature and the revocation
  records.** It unloads a skill when the skill is revoked or replaced.
- **`call_skill` carries the hash the broker expects.** The host returns `not_loaded`
  for a skill it doesn't have, and `stale` for one whose hash differs. The broker then
  reloads the skill. This detects drift, such as a restarted host or a stale load. It
  isn't tamper protection (see B7).
- **`eval` is offered only to principals the broker grants it** (see
  [emacs-lisp.md](emacs-lisp.md)).

## Building it

Each clause is built with the work that needs it, in the order of
[../../roadmap.md](../../roadmap.md):

1. A1, A3, A4 and A5 with writes ([writes.md](writes.md)): a changeset's precondition
   is the version a read reported.
2. A6, B4, B5 and B6 with connectors ([connectors.md](connectors.md)) and the queue's
   registration item ([queue.md](queue.md)).
3. The Unix-socket transport, B2 and B3, with a Linux host.
4. The runtime host profile with Emacs Lisp skills ([emacs-lisp.md](emacs-lisp.md)).
