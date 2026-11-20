# maji-mcp documentation

Where each document is, what each document covers, and how the documents are written.

## Start here

| To learn | Read |
|---|---|
| what maji-mcp is for, and how to start it | [../README.md](../README.md) |
| how to run it day to day, and fix what goes wrong | [using.md](using.md) |
| how the parts fit together | [design/architecture.md](design/architecture.md) |
| what a term means | [design/glossary.md](design/glossary.md) |
| what protects what, and what is not protected | [design/security.md](design/security.md) |
| how to build, test and debug a change | [development.md](development.md) |
| what comes next | [roadmap.md](roadmap.md) |
| why a choice was made | [decisions.md](decisions.md) |

## What is built: `design/`

Every document in `design/` describes the running system. Each ends with a short
"Planned" section that points to the work designed for that area.

| Document | Covers |
|---|---|
| [architecture.md](design/architecture.md) | the components, the connections between them, the trust boundaries, the path of a script |
| [glossary.md](design/glossary.md) | every term, with one meaning each |
| [security.md](design/security.md) | each protection and what it rests on; what is not protected |
| [gateway.md](design/gateway.md) | the tools agents call, principals, sessions, the headers helper and the relay |
| [runner.md](design/runner.md) | the sandbox, the runtime image, mounts, what a mount leaves out, paths never mounted |
| [broker.md](design/broker.md) | stubs, the checks on each call, services, the bridge, saved values, the run log |
| [skills.md](design/skills.md) | keeping, calling, promoting and demoting a skill; the library |
| [connections.md](design/connections.md) | the keys you paste for services scripts call, where each key is kept, and where it never is |
| [control-plane.md](design/control-plane.md) | items, risk, deciding, grants, passkeys, the REST API, the database |
| [client-rules.md](design/client-rules.md) | what every queue client shows, and how deciding works in a client |
| [deployment.md](design/deployment.md) | containers, volumes, ports, secrets, images, who owns the Docker daemon |
| [agent-guidance.md](design/agent-guidance.md) | when code beats a series of tool calls, and how agents learn that today |
| [server-contract.md](design/server-contract.md) | what the broker and the bridge do for an MCP server today, clause by clause |
| [mockups/decision-queue.html](design/mockups/decision-queue.html) | a clickable mockup of the queue, from before the web client |

## What is planned: `design/planned/`

Every document in `design/planned/` describes a design that is not built, and ends
with the order to build it in. [roadmap.md](roadmap.md) orders the documents.

| Document | Covers |
|---|---|
| [connections.md](design/planned/connections.md) | signing in to remote MCP servers with OAuth, and logs from cloud storage |
| [adding-connectors.md](design/planned/adding-connectors.md) | adding a REST API, an MCP server, a database, a fetcher or code of your own as a connector, step by step |
| [connectors.md](design/planned/connectors.md) | how connectors work: the record, lookups, the launcher's signature check, each call, fetches, the REST API |
| [writes.md](design/planned/writes.md) | writes as changesets, preconditions, policy rules |
| [queue.md](design/planned/queue.md) | questions, the advisor, more findings about risk, risk analysis by a security tool, server registration |
| [server-contract.md](design/planned/server-contract.md) | the rest of the server contract: versions and preconditions, a Unix-socket transport, registration, the runtime host profile |
| [agent-guidance.md](design/planned/agent-guidance.md) | hooks, a Claude Code skill, tool notes, and guidance that improves from the run log |
| [clients.md](design/planned/clients.md) | an Emacs client, a command-line client, a macOS app |
| [library-view.md](design/planned/library-view.md) | a page that shows kept, promoted and imported skills |
| [sharing.md](design/planned/sharing.md) | signatures, sync within a persona, packs between personas |
| [languages.md](design/planned/languages.md) | skills in languages other than Python |
| [emacs-lisp.md](design/planned/emacs-lisp.md) | skills in batch Emacs and in your live Emacs |
| [emacs-mcp.md](design/planned/emacs-mcp.md) | notes for the separate Emacs MCP server project |

## Where the design came from: `research/`

Records from before and during the first build. Each opens with a note on what has
changed since. The measurements are kept as a summary: the harness and the per-run
numbers held private data, and are not in the repository.

| Document | Covers |
|---|---|
| [code-mode-notes.md](research/code-mode-notes.md) | the Code Mode idea maji-mcp builds on |
| [brainstorm-review.md](research/brainstorm-review.md) | the review that set the trust zones |
| [evaluation.md](research/evaluation.md) | whether the concept holds up, and where it does not help |
| [measurements.md](research/measurements.md) | what an agent's work cost with and without code, on six real tasks: the method and the results |

## How the documents are written

These rules keep the documents easy to read and easy to check against the code.

- **One status for each document.** A document in `design/` describes what is built.
  A document in `design/planned/` describes what is not. A document says its status
  in its first lines.
- **One meaning for each term.** [design/glossary.md](design/glossary.md) defines the
  terms. A new term goes into the glossary in the commit that first uses the term.
- **A sentence names its subject.** Write "The broker checks the policy", and repeat
  "the broker" in the next sentence. A reader should never have to look back to find
  what "it" or "this" refers to.
- **"You" is the person who runs the instance.** Text for agents calls the same
  person "the user".
- **Present tense for what is built, "will" for what is planned.**
- **Name the code.** A statement about behaviour names the file that holds the
  behaviour, as a path from the repository's root.
- **Say what a thing does.** Leave out what the thing used to do, and what the thing
  is not.
- **Short paragraphs, and lists whose items start with the point in bold.**
- **Your data stays out.** Examples use made-up names for projects, hosts and
  people.
