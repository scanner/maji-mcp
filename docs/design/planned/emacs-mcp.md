# Planned: the Emacs MCP server

Notes for a separate project.

**Status:** planned. The project does not exist yet. These notes seed it.

The Emacs MCP server is a separate project: an Emacs package that runs an MCP server
inside your Emacs, as you. maji-mcp's side of the arrangement is the server contract:
the clauses built today ([../server-contract.md](../server-contract.md)) and the rest
([server-contract.md](server-contract.md)). The server implements Layer A, Layer B and
the runtime host profile for `elisp-live`. These notes collect what is specific to
Emacs, to seed that project.

## Why it exists

The broker runs in a container, on the container VM's kernel. Your Emacs server socket
is private to your user on macOS, so the broker can't use `emacsclient`. A server inside Emacs runs with exactly the
access Emacs already has. To the broker it is one more MCP server behind the same RBAC,
rate limits and run log as the rest. It is separate from claude-code-ide.el's servers,
though it borrows ideas from them.

## Lifecycle

- **It starts from your init file and runs whenever Emacs runs,** independent of any
  Claude session. claude-code-ide's emacs-tools server, by contrast, starts with the
  first Claude session and stops with the last.
- **It writes its registration file at start and removes it on a clean stop** (contract
  B4).

## Tools beyond the profile

The runtime host profile covers `load_skill`, `call_skill`, `list_skills`,
`unload_skill` and `eval`. On top of that, the Emacs server offers:
- **Inspection:** buffers, the current project, modified buffers, diagnostics, xref,
  imenu, tree-sitter. These are the facilities claude-code-ide exposes, here returning
  data with versions (contract A3).
- **Presentation:** open a file at a line, open a report buffer, highlight a region,
  open a diff. These are the ways a skill shows you a result.
- **Buffer edits with preconditions** (contract A4). A buffer's version can be its
  modification tick, `buffer-modified-tick`, together with the file it visits.

## Implementation notes

- **Transport on a Mac:** streamable HTTP on `127.0.0.1` with a token (contract B2),
  because the broker runs in a container and can't reach a socket on macOS. The
  `web-server` package that claude-code-ide uses serves HTTP from inside Emacs.
- **Transport on Linux, optionally:** `make-network-process` with `:family 'local` and
  `:server t` serves a Unix socket. A process filter splits the stream into
  newline-delimited JSON-RPC messages (contract B2).
- **Permissions:** Emacs creates the socket with your umask. A test gave
  `srwxr-xr-x`. The runtime directory is the gate, not the socket (contract B3).
- **Path length:** a 117-byte socket path failed with "Service name too long". Use the
  short runtime directory.
- **One thread:** requests run on Emacs's main thread between your commands, one at a
  time, so the registration declares `concurrency: 1`. `with-timeout` didn't interrupt
  a busy loop in a test, so the rules in [emacs-lisp.md](emacs-lisp.md) apply: keep
  calls small.
- **JSON:** `json-serialize` for results. Errors go into the contract's
  `{code, message, data}` shape, not message strings.

## emacs-org-mcp

emacs-org-mcp acts as you too: it writes your org files and calls `emacsclient`. It
stays as it is. maji-mcp reaches it through a host-side bridge that serves it over HTTP
with a token, and builds finer operations, such as ticking one checklist item, as skills
over its existing tools.

The Emacs MCP server offers a second way to do the same operations: elisp skills that
use org's own functions on the live buffer. `org-toggle-checkbox` and
`org-update-checkbox-count` keep the progress cookie right, and the buffer you're looking
at stays current. Those skills have to do what emacs-org-mcp does for its own writes:
set `:MODIFIED:`, save, and leave the git commit to the host.

## Prior art

- **claude-code-ide.el:**
  - its emacs-tools HTTP server, built on the `web-server` package
  - `claude-code-ide-make-tool`, for declaring tools with typed arguments
- **mcp-server-lib.el:** a library for writing MCP servers in elisp, which
  `elisp-dev-mcp` is built on. It talks to clients through a stdio transport script. Any
  stdio process the broker launches runs in the broker's container, not as you. So
  check how that script reaches the running Emacs, and whether the library can serve
  the contract's HTTP transport instead.
