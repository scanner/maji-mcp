# Roadmap

What is built, what comes next, and the order of the work. Each piece of planned work
points to the document that designs it.

## How to use this document

- **`- [ ]` is open, `- [x]` is done.** When an item is done, check the item off.
- **Describe built behaviour in the design documents, in the same commit.** When a
  planned document's content is built, move the content from `design/planned/` into
  the matching document in `design/`.
- **Record a decision in [decisions.md](decisions.md)** on the day the decision is
  made.
- **Each section has a goal and a "done when".**
- **An item in another repository is labelled,** such as `[emacs-mcp]`.

## Built

| Area | What works | Design |
|---|---|---|
| Measurement | Six real tasks measured in two modes, with a decision to build | [research/measurements.md](research/measurements.md) |
| Scripts in a sandbox | `run_python`, one executor for each run, saved values, parallel stub calls | [design/runner.md](design/runner.md) |
| Stubs | Sentry, Jira and GitHub over their REST APIs, read-only; emacs-org-mcp through a bridge, read-only, with records parsed from its text | [design/broker.md](design/broker.md) |
| Policy, rate limits, the run log | A policy file for each principal; one run-log row for each run, each stub call, and each runtime library a run used | [design/broker.md](design/broker.md) |
| Runtime libraries | YAML, Jinja, Markdown, tree-sitter grammars, diffs, fuzzy matching, duckdb and pandas in the runtime image, each with notes agents read through `read_api`; `make library-use` | [design/runner.md](design/runner.md), [design/agent-guidance.md](design/agent-guidance.md) |
| Apps | Claude Code through a headers helper; Claude Desktop through a relay; sessions for both | [design/gateway.md](design/gateway.md) |
| Mounts | Granted paths read-only in the executor; a project as one grant, with files named like secrets and your never-mount entries covered; paths never mounted; git and SQLite helpers | [design/runner.md](design/runner.md) |
| The queue | Items, risk, decisions, grants with patterns, expiry, the event stream, backups, a demo queue | [design/control-plane.md](design/control-plane.md) |
| Signing | Passkeys, recovery codes, deciding unlocked in a tab | [design/control-plane.md](design/control-plane.md) |
| The web client | Queue, item, decision form, one choice for every part of a request, batches, grants, passkeys; keyboard and screen-reader use | [design/client-rules.md](design/client-rules.md) |
| Skills | Keep, call, promote and demote, on one instance | [design/skills.md](design/skills.md) |
| Connections | A key pasted into the web app for each service that takes one, kept by the broker alone; Sentry, Jira and GitHub connected that way | [design/connections.md](design/connections.md) |
| Bring-up | `make up`, `make connect-claude-code`, `make connect-claude-desktop`, `make bridge` | [using.md](using.md) |

The documents in `research/` name the work by stage. Stage 0 was the measurement.
Stage 1 was scripts, stubs and the gateway. Stage 2 was the control plane, of which
writes and connections remain. Stage 3 was skills and sharing, of which the library on
one instance is built. Stage 4 was other languages and the live Emacs.

## Going public

**Before the first public commit:**

- [x] A license: BSD 3-Clause (`LICENSE`)
- [x] A README written for a reader with none of the authors' context: platforms,
  what each command produces and where, and how to run what was built
- [x] Made-up names in every example, fixture, mockup and design
- [x] Rules for contributors and their coding agents (`AGENTS.md`, `CLAUDE.md`,
  `CONTRIBUTING.md`), how to report a vulnerability (`SECURITY.md`), and a changelog
  (`CHANGELOG.md`)
- [x] Any Docker on a Mac: the commands read the Docker CLI's current context and the
  socket's group
- [ ] The public repository starts from a fresh history, with its first commit made
  under the name and email its authors publish under. The private history is the
  record of a proof of concept, and holds names that were later replaced.
- [ ] `make test-all` passes on the first public commit
- [x] The repository's address, `https://github.com/scanner/maji-mcp`, in
  `pyproject.toml`, `SECURITY.md` and `CONTRIBUTING.md`
- [ ] Private vulnerability reporting turned on, once the repository exists

**With the public repository:**

- [x] Continuous integration, on Drone: lint, types and tests on every push
  (`make check`, `make test`, `make web-check`), and on a tag, both images built and
  published
- [ ] Linux, as a host for the stack. People who join may not use a Mac, and CI runs
  on Linux, so the Docker tests wait on it too. The first implementation is for
  macOS, and the design keeps Linux open ([design/deployment.md](design/deployment.md),
  option (c)). Known gaps:
  - `host.docker.internal`, which the broker uses to reach the bridge and Docker on
    Linux does not define by default
  - Claude Desktop's settings path in `make connect-claude-desktop`
  - what the runner's and the executors' users may read of a bind mount, where no
    VM's file share sits between the container and your files
  - the Unix-socket transport for servers that act as you, which a shared kernel
    allows ([design/planned/server-contract.md](design/planned/server-contract.md), B2)

## 1. Reach: connections

**Goal:** maji-mcp holds its own credentials for more services, so scripts and skills
can join them. **Design:** [design/planned/connections.md](design/planned/connections.md);
what is built is in [design/connections.md](design/connections.md).

**Done when:** a script joins Jira, git history and Sentry through `run_python`, with
no credential in the script's reach.

- [x] The credential store in the broker
- [x] Service definitions for REST APIs that take a key
- [x] The "Connect <service>" item, and the web client's form for a key
- [x] The Connections page, and replacing or disconnecting a key from its item
- [x] Sentry moved onto a connection: a key pasted in the web app, in place of the
  token files
- [ ] OAuth sign-in to remote MCP servers, with the control plane's callback route
- [ ] Sentry's MCP server as a second namespace, over OAuth
- [ ] Atlassian's MCP server checked for what a script gets from it
- [x] Jira and GitHub stubs, read-only, over keys pasted in the web app
- [ ] Disconnecting a service by name (`DELETE /connections/{service}`)
- [ ] Logs from cloud storage, through a cache volume
- [x] The `/connections` routes that list services and ask to connect one
- [ ] The `/oauth/callback` route of the contract
- [ ] Connectors you add yourself: MCP servers, REST and database definitions,
  and code of your own, each approved in the queue
  ([design/planned/adding-connectors.md](design/planned/adding-connectors.md),
  [design/planned/connectors.md](design/planned/connectors.md))

Examples that drive the work. Each example becomes a measured task, a test on a
synthetic fixture, and later a skill. Real captures, profiles and repositories are
your data and never enter the repository. A `SKILL.md` that comes from a mounted
folder, such as a capture tool's notes for agents, is text from that folder: like a
tool note, the text guides agents only once you accept it.

| Example | What it joins | Needs |
|---|---|---|
| **Frames to commits:** for the top production issues, the in-app frame's file and line, and the commit that last changed the line | Sentry × git blame | nothing new |
| **Hotspots:** the files with the most commits in 90 days across repositories, with the production issues that point at each | git log × Sentry | nothing new |
| **Org tasks to branches:** active tasks that name a branch or ticket, and when the branch last moved | emacs-org × git | nothing new |
| **A log capture's errors by code site,** each with the commit that last changed the line and the Sentry issue for the same culprit | SQLite × git × Sentry | nothing new for a finished capture; a live capture needs a query on your machine |
| **Profiles:** time spent in each function, and what changed between two profiles | files only | pprof and memray need libraries in the runtime image |
| **Code:** every call to a named function, found with `ast` | files only | nothing new |
| **Dependency drift:** packages pinned at different versions across repositories' lockfiles | files only | nothing new |
| **Tickets to commits to issues:** resolved tickets whose commits changed a file a production issue points at | Jira × git × Sentry | nothing new |
| **Logs from a bucket:** filtered and counted | storage × code | a connection and the cache volume |

## 2. Writes: changesets

**Goal:** a script's write waits in the queue, and lands only after your decision and
only if the target has not changed. **Design:** [design/planned/writes.md](design/planned/writes.md).

**Done when:** an agent's emacs-org edit is applied after approval in the web client
and appears in the history, and a stale changeset comes back to the agent as a
conflict.

- [ ] Write stubs record a changeset, with the version the script read
- [ ] The broker computes the item's facts: the diff, the stubs called, the size
- [ ] An approved changeset is applied through the decision feed, with the
  precondition checked again
- [ ] A re-read precondition for servers without `if_match`, such as emacs-org-mcp
- [ ] Retry rules for a write that timed out
- [ ] Policy rules, version 0: structural changes you asked for, and a checked tick
- [ ] Coalescing several changesets to one target into one item
- [ ] The bridge's emacs-org-mcp runs with `--no-ediff-approval`
- [ ] The first skills that write: checklist-item operations and a section edit over
  emacs-org-mcp's tools; creating and updating Jira tickets
- [ ] Repository writes: a bundle from the run, landed as a branch or a worktree
- [ ] Reverting an applied changeset, as a reconsideration
- [ ] Write scopes on a connection, as a separate item

## 3. The queue grows

**Goal:** the queue carries questions and server registrations, and gives you more
help deciding. **Design:** [design/planned/queue.md](design/planned/queue.md).

**Done when:** an agent's question is answered from the queue, and a new MCP server
cannot be used until you approve its registration.

- [ ] Gateway tools for an agent's own items: `submit`, `status`, `withdraw`
- [ ] Questions as items, from agents and from MCP servers' elicitation requests
- [ ] Server registration and schema changes as items, replacing
  `config/servers.json`; the broker watches the registration directory and
  reconnects
- [ ] Grants from the control plane in place of the broker's policy file
- [ ] The remaining risk reasons: data leaves, your words, text addressed to an AI,
  reach beyond the task
- [ ] Your words on each item, from a hook in the agent's app
- [ ] The advisor's MCP endpoint
- [ ] One notification for everything waiting
- [ ] The resource view (`/resources`)
- [ ] A session explained in the client, with a way to end it
- [ ] A request partly covered by grants comes with only the uncovered parts
- [ ] Marks for what is sensitive to you; corrections to a risk label
- [ ] An optional prompt-injection detector
- [ ] Risk analysis of a waiting item by a security tool, such as Corridor, through
  one interface that names no tool
- [ ] Measures that the queue stays readable; policy-rule proposals from approval
  rates (`/policy/proposals`)
- [ ] Your never-mount list in the web client
- [ ] Waiting a few seconds for a decision, as an option

## 4. Guidance that reaches agents

**Goal:** an agent notices early that code or a skill is the better tool, and the
guidance improves from what the run log shows. **Design:**
[design/planned/agent-guidance.md](design/planned/agent-guidance.md); what is built is
in [design/agent-guidance.md](design/agent-guidance.md).

- [ ] Tool descriptions worded in terms of the signals, measured against the runs
  before the change
- [x] The run log records which runtime libraries each run imported, asked `help()`
  about, and failed in, and `make library-use` counts them
- [ ] A measured task run with and without the runtime libraries' notes
- [ ] The run log records each skill's measured saving and first-time cost
- [ ] Claude Code hooks: a nudge before an expensive call, after a large result, and
  at a count of repeated calls
- [ ] Tool notes, each accepted through the queue
- [ ] Proposals for skills, from writes that sent far more than they changed
- [ ] A Claude Code skill that teaches the signals
- [ ] A Claude Code plugin that bundles the gateway's configuration, the skill and
  the hooks
- [ ] Skills as gateway tools of their own, found by search
- [ ] Generated typed stubs for Python

## 5. Sharing across machines and personas

**Goal:** skills move from one instance to signed, shared tools across your machines
and personas. **Design:** [design/planned/sharing.md](design/planned/sharing.md).

**Done when:** a version kept on the laptop is promoted from the desktop and runs on
both, and a pack moves from home to work and runs under the work persona's signature.

- [ ] Promotion signatures the broker verifies; signed demotions
- [ ] A manifest that carries `runtime`, input and output schemas, a lockfile and
  `derived_from`
- [ ] The library read through a source interface
- [ ] A run policy for each instance
- [ ] File sync within a persona
- [ ] Replication of items and decisions within a persona
- [ ] Shared configuration within a persona
- [ ] Packs: extract, export review, import, pin, fork, upstream changes
- [ ] A metrics sink for each persona

## 6. Other languages and the live Emacs

**Goal:** skills in a second language, and Emacs Lisp skills through the broker.
**Design:** [design/planned/languages.md](design/planned/languages.md),
[design/planned/emacs-lisp.md](design/planned/emacs-lisp.md).

**Done when:** one skill in a second language runs through the broker, and one
`elisp-live` skill runs through the broker.

- [ ] Build and run image pairs, and the build container's settings
- [ ] The build cache, with entries signed by runner keys
- [ ] A dependency mirror and an allowlist for each ecosystem
- [ ] A client library for the second language
- [ ] Calls between skills through the runner
- [ ] The `elisp-batch` runtime image
- [ ] Promotion policy for each language
- [ ] `elisp-live` through the Emacs MCP server's runtime host profile

## 7. Clients, deployment and hardening

**Design:** [design/planned/clients.md](design/planned/clients.md),
[design/deployment.md](design/deployment.md).

- [ ] The web client's gaps: the compact view's one-line rows, showing a new passkey,
  a request's parts before its claim, finding a grant by its session's name
- [ ] The web client tried in Safari and Firefox, and with VoiceOver
- [ ] The Emacs queue client, after writes
- [ ] A command-line client
- [ ] A page that shows the skill library: kept, promoted and imported skills, grouped
  by area, state, origin and bundle ([design/planned/library-view.md](design/planned/library-view.md))
- [ ] A `connect-<app>` command for each further app worth one
- [ ] The bridge started at login, as a user service, so it is running after a
  restart of the machine
- [ ] Option (c): a remote host, with TLS or a private network in front of the
  gateway, and a guide to the control plane's name and certificate
- [ ] Option (b): a container VM under a separate macOS user
- [ ] A hardened agent profile: no shell, with file tools limited to chosen
  directories
- [ ] A secret store with short-lived, scoped tokens
- [ ] gVisor for executors on a Linux host
- [ ] A warm container for each runtime, if the time to start an executor becomes a
  cost

## Smaller open items

- [x] A runner that starts removes the executors left by a runner that crashed or was
  killed outright
- [ ] An item that asks for a folder and a folder inside it says that refusing the
  inner folder protects nothing
- [ ] Files git ignores, named on a mount item
- [ ] A live capture queried on your machine, through a bridge, read-only
- [x] Runtime libraries for code, text and data, with notes for agents
- [ ] More runtime libraries, each added once a task or `make library-use` shows the
  library pays
- [ ] A remembered look, so a large folder is not listed again before every run
- [ ] A covered file that reads as a regular empty file, in place of the empty device
- [ ] Folders named like secrets, such as `secrets/`, covered whole
- [ ] The second measurement's output gap: scripts through `run_python` wrote 17%
  more output than the baseline script mode on one task
- [ ] A harness for the next round of measurement, on made-up tasks and data that can
  live in the repository

## Separate projects

- **`[emacs-mcp]` The Emacs MCP server** ([design/planned/emacs-mcp.md](design/planned/emacs-mcp.md))
  - [ ] Create the repository and seed it from the notes
  - [ ] Layer A of the server contract: structured output, versions, `if_match`,
    error codes ([design/planned/server-contract.md](design/planned/server-contract.md))
  - [ ] Layer B: HTTP with a token on a Mac, a registration file
  - [ ] The runtime host profile
  - [ ] Inspection and presentation tools
- **emacs-org-mcp:** no changes. maji-mcp uses emacs-org-mcp as it is, through the
  bridge.
- **The macOS app** ([design/planned/clients.md](design/planned/clients.md)), after
  the web client
  - [ ] The menu bar panel
  - [ ] The widget
