# Decisions

The decisions that shaped maji-mcp, by date, each with the reason and the document that
now holds the result. The design documents say how maji-mcp works. This document says
why maji-mcp works that way, and when each choice was made.

**How to use this document.** Add a decision under its date when the decision is made.
Change the design document in the same commit. A decision that replaces an earlier
decision says so, and the earlier entry stays.

## 2026-11-20

- **The public repository holds made-up names only, and starts from a fresh
  history.** The proof of concept was built against one person's services, and its
  examples and measurements grew from that work. Examples, fixtures, mockups and
  designs now use invented services and data, and the worked example of a large
  dataset is a made-up weather-station archive
  ([design/planned/adding-connectors.md](design/planned/adding-connectors.md)).
- **The measurement harness is removed, and its results are kept as a summary.** The
  harness, the prompts and the per-run numbers answered the question they were built
  for, and they were tied to private data. [research/measurements.md](research/measurements.md)
  keeps the method and the totals. The next round of measurement will bring a harness
  of its own, on made-up tasks that can live in the repository. One part of the
  harness is kept: `complete_task_item`, the skill that checks off an item through
  emacs-org-mcp, now in `tests/local/org_skills.py` with tests, as the code the
  first write skills start from ([design/planned/writes.md](design/planned/writes.md)).
- **A document is built or planned, never both.** The server contract and the agent
  guidance each described built and planned behaviour in one document. Each is now
  two: the built part in `design/`, and the rest in `design/planned/`. The contract's
  clauses keep their numbers across both
  ([design/server-contract.md](design/server-contract.md),
  [design/agent-guidance.md](design/agent-guidance.md)).
- **Risk analysis by an outside tool goes through one interface that names no
  tool.** A tool such as Corridor can judge the risk of a waiting item. The control
  plane will give any such tool the item as you see it, and show the tool's findings
  as evidence beside the broker's facts. A finding never decides an item, and sending
  items to a hosted tool is a connection you approve
  ([design/planned/queue.md](design/planned/queue.md)).
- **The commands ask Docker where its daemon and its socket's group are.** The
  Makefile assumed Rancher Desktop's socket path and group. `make` now uses the Docker
  CLI's current context, and `make up` reads the socket's group from the socket, so
  Docker Desktop and Colima need no settings. Rancher Desktop is still the only one
  tested ([design/deployment.md](design/deployment.md)).
- **Rules for coding agents live in the repository.** `AGENTS.md` holds the rules for
  comments, docstrings, tests and documents, with examples, and `CLAUDE.md` imports
  it, so every contributor's agent reads the same rules.

## 2026-11-16

- **Scripts get runtime libraries for code, text and data.** An agent moved an
  analysis out of maji-mcp because the sandbox had no YAML or Jinja parser. The set
  covers parsing (PyYAML, Jinja2, markdown-it-py), code (Pygments, tree-sitter with
  grammars for the languages in your repositories, unidiff, pathspec), matching
  (RapidFuzz) and data (duckdb, pandas). pandas is in to see whether agents reach for
  it when they sift data; duckdb does the same group-bys with less memory. The
  tree-sitter grammars are separate packages, chosen by language, because the pack of
  all 170 grammars unpacks to 199 MB. lxml, regex, dateutil and libcst stayed out: the
  standard library, or a library already in, covers them for reading
  ([design/runner.md](design/runner.md)).
- **Agents learn the runtime libraries from notes, and `help()` for the rest.** Each
  library has a short entry, `read_api("python.<name>")`, saying where to start and
  what to watch for. A tool that explains a library would start an executor to read
  its documentation, which a script's `help()` does already.
- **The run log measures whether the notes are enough,** before anything more is
  built: each run's imports, `help()` calls and the library a failure came from, and
  each read of the notes ([design/agent-guidance.md](design/agent-guidance.md)).
- **The runtime lock takes only wheels,** as the image installs only wheels. With
  compiled libraries in the image, a lock that allowed a source release could pin a
  version the image cannot install.
- **`make library-use` counts only the runs that reported what they used.** Runs from
  before reports began can't show an import, and counting them made agents look as if
  they never used a library. The run log marks each run that reports. In a run log
  from before the mark, a run that reported a runtime library is marked from its
  rows; one that reported and used none left no trace, and counts as not reporting.
- **Jira is read through its REST API with a scoped key you paste,** not through
  Atlassian's MCP server. The server takes an API token only once an administrator of
  your Atlassian organization turns that on, and otherwise signs a client in with
  OAuth, which isn't built. A scoped token with `read:jira-work` can only read, and
  goes only to Atlassian's API gateway. Checking the MCP server stays planned, for a
  second namespace ([design/connections.md](design/connections.md)).
- **GitHub is read with a fine-grained token,** which names its repositories and can
  be limited to reading. The broker follows next-page links only to the configured
  API, and reports a redirect as an error, so the key goes to no other host.
- **The Jira and GitHub stubs return records:** names in place of nested objects, and
  Jira's rich text as plain text. Their objects nest users, statuses and documents
  several levels deep, and a script would otherwise unpack the same fields every time.
- **A database still being written is read through snapshots its writer publishes,**
  each a whole copy under a name of its own, with a link to the newest. Reading the
  live file through a mount gave stale rows, with no error. A log capture tool now
  publishes snapshots this way, and `maji.sqlite` reads them as they are
  ([design/runner.md](design/runner.md)).

## 2026-11-13

- **The documents are split by status.** `docs/design/` describes what is built.
  `docs/design/planned/` describes what is designed and not built.
  `docs/research/` holds the notes and measurements from before the build, kept as
  written. A reader then knows from a document's place whether the document describes
  the running system.
- **The roadmap is ordered by area, in place of numbered stages.** The work stopped
  following the stage numbers: the skill library was built before the second stage
  finished. [roadmap.md](roadmap.md) maps the old stages to the new order.
- **The Python package is grouped by where code runs.** `host/` holds the programs
  that run on your machine as you. `executor/` holds the code that runs inside the
  sandbox.
- **The Docker tests build and run their own image tag.** A test run no longer
  replaces the `latest` images a running stack uses.
- **The secrets directory is outside every project,** at `~/.config/maji-mcp/secrets`.
  The runner refuses a folder that holds the secrets directory, so secrets inside the
  checkout meant a script could not be granted the checkout as one path. A hidden
  folder at the top of your home directory is never mounted at all.
- **No default path is relative to the current directory.** A program's default
  location is tied to the project or to your home directory, or is a declared
  setting. The setup command's two relative defaults were removed.
- **A command that connects an app is named for the app:** `connect-claude-code`,
  `connect-claude-desktop`. maji-mcp is for any app that speaks MCP, and a bare
  `connect` read as if Claude Code were the only one.
- **The license is BSD 3-Clause.**
- **The public repository will start from a fresh history.** The private history
  records a proof of concept built quickly, and has no value to a reader of the
  published project.
- **A project is one grant.** An agent that works in a repository already reads
  everything in the repository through its own app. Asking again for each folder puts
  hoops in front of access the agent already has, and makes the agent less likely to
  reach for code. One request for a repository is reasonable, and treating every
  repository the same way, the agent's own included, keeps the rule simple.
- **A mounted folder leaves out the files named like secrets,** and the entries of
  your never-mount list inside it. A script reads each as empty
  ([design/runner.md](design/runner.md)). Before, a `.env` in a project made the
  grant high risk, and one listed path inside a project refused the whole project.
- **A file named like a secret is read only on a grant of that very file,** asked for
  by name, as a high-risk item. A grant on a folder was never a grant to read what
  the folder's mount leaves out.
- **A folder that cannot be checked is not mounted.** When the look for files named
  like secrets cannot finish within its bounds, some could go uncovered, so the run
  is refused, and the agent asks for a folder inside it. maji-mcp's own secrets
  directory stays an outright refusal, never a cover.
- **One choice sets how long every part of a request is remembered.** Granting nine
  paths took nine choices.
- **A name that is no stub's is answered with the stubs the principal may call.**
  The `not_found` answer listed every stub of the namespace, so a principal granted
  one stub learned the names of the others by misspelling a call. A stub that exists
  and is outside the principal's grants still answers `permission_denied`, so the
  run log keeps recording the attempt ([design/broker.md](design/broker.md)).
- **A principal's name is letters, digits, hyphens and underscores.** The name is
  also the name of the principal's token file, and `maji-setup` used the name
  unchecked: a name such as `../x` wrote a token file outside the secrets directory,
  and the name `emacs-org` replaced the bridge's token. Every `maji-setup` command
  now checks the name before the command touches a file ([using.md](using.md)).
- **The bridge serves only behind a token file that is yours alone and holds a
  token.** The bridge used an existing token file as the file was: a file others
  could read stayed readable, and an empty file gave an empty token. The bridge now
  narrows the mode of such a file, and does not start on a file with no token
  ([design/server-contract.md](design/server-contract.md), B2).
- **Every note reaches an agent as one line of printable text.** Only a demotion's
  note was cleaned. A note on "request changes" or "reject" reached the agent with
  its line breaks and control characters ([design/skills.md](design/skills.md)).
- **A server's id is letters, digits, hyphens and underscores,** for the reason a
  principal's name is: the bridge names the server's token file and registration by
  the id ([design/server-contract.md](design/server-contract.md), B4).
- **Each service stops by itself on a stop signal.** The broker and the runner
  handled no stop signal, and a container's first process is sent a signal only if
  the process handles the signal. Docker therefore waited 10 seconds and killed both
  on every restart. The gateway waited without limit for the streams apps hold open,
  and was killed the same way
  ([design/architecture.md](design/architecture.md), Stopping).
- **A runner told to stop ends its scripts first.** A killed runner left the
  executor of a script in flight running, with nothing to hold the script to its
  deadline or to remove the container. A runner told to stop ends every script in
  flight, and the agent is told to run the script again. Scripts only read, so ending
  one loses nothing but time. A runner that crashes or is killed outright can still
  leave an executor behind ([design/runner.md](design/runner.md)).
- **Sentry stays on its REST API,** and its MCP server is to be a second namespace for
  the tools the REST stubs don't cover. A spike on 2026-11-13 ran the same questions
  through both on real data. The MCP server now returns structured content, runs exact
  Sentry search syntax unchanged, and agreed with the REST API on issues, culprits,
  environments and frames. It is still wrong for scripts: an issue search returns 100
  issues at most, with no cursor and no sign that more exist; with an environment
  filter, an issue's count is every environment's, which gave production counts up to
  41 times too high; and a model runs on every search, 4 to 11 seconds a call
  ([design/connections.md](design/connections.md)). An OAuth token from Sentry's MCP
  server works only there, so it cannot feed the REST stubs.
- **You paste a service's key into the web app.** No one edits a file with a secret in
  it. The Sentry token and organization files in the secrets directory are retired;
  `make up` no longer reads `SENTRY_AUTH_TOKEN` or `sentry.env`.
- **A pasted key reaches only the broker.** The key goes beside the Connect decision you
  sign, which names the key's SHA-256. The control plane holds the key in memory until
  the broker takes it, so no key is in the queue's database or its backups. The host a
  key is sent to comes from the broker's configuration, never from a form, and the
  decision's signed content covers it. A key the service refuses is not kept
  ([design/connections.md](design/connections.md)).
- **A runner that starts removes the executors an earlier runner left,** which closes
  what the entry above leaves open. A crashed runner is started again by compose, so
  the removal needs nobody to notice. The runner knows its own executors by the
  socket volume of its broker, because a label alone would also match the executors
  of another stack on the same Docker daemon.

## 2026-11-11

- **The skill library comes before reaching more services.** Agents then get what
  code mode is for before maji-mcp reaches more services: a function written once is
  called, never written again. Reach comes next: connections to Jira, GitHub and
  other MCP servers, so skills can join them ([roadmap.md](roadmap.md)).
- **Keeping a skill needs no decision.** An agent writes and refines throwaway scripts
  all the time, and a skill runs with the caller's permissions. Keeping a skill
  therefore gives the agent nothing a script does not ([design/skills.md](design/skills.md)).
- **Promotion is only for sharing or for writes.** An agent asks for promotion when
  you want a skill shared with other agents, or for a skill that writes.
- **What a skill reaches sets the risk, never the skill being code.** Whether a stub
  reads or writes is decided once, when the stub's service is set up, and the broker
  checks every call. maji-mcp therefore need not infer anything from a script.
- **Each run's result names what the run called,** from the run log. You see in the
  agent's transcript which tools a script used, with nothing to decide.
- **Demoting stops the version for every agent, the agent that kept the version
  included.** All of one person's agents may share a principal, so a demotion that
  left the keeper its version would change nothing. Demoting only narrows what agents
  can do, so an unlocked tab signs a demotion, even on a high-risk promotion.
- **A session is named in three words,** made from the session key. A 64-character
  key cannot be told from another at a glance. The name is the same wherever the name
  is computed, with no table to keep. The name is a label, never an identity
  ([design/gateway.md](design/gateway.md)).
- **Claude Desktop connects through a relay.** Claude Desktop starts local servers
  only as commands, and its connectors reach servers from Anthropic's cloud, which
  cannot reach `127.0.0.1`.
- **In a narrow window, an item opens in its row** ([design/client-rules.md](design/client-rules.md), 1.14).

## 2026-11-10

- **A session's grant can end at a time.** An agent at work all day keeps its session
  all day, so "8 hours" ends the session's grant with the working day. The grant
  covers no more than the session would, so an unlocked tab can sign it.
- **An app can name its session.** MCP's 2026-07-28 revision, which Claude Code speaks,
  has no sessions: each request stands alone. Without a session, a mount could only
  be kept for a time, a refusal held across every conversation, and `maji.save`
  failed. An app therefore names a session in the `X-Maji-Session` header.
- **A grant on a folder covers everything in the folder,** including a folder inside
  it asked for later. A later request mounts less than you granted.
- **A refusal covers exactly the path refused,** unlike a grant. A folder refused as
  too broad does not stop a request for the folder you then point the agent to.
- **You can let an agent ask again for a path you refused,** from the decided item,
  once you have told the agent more. Only you, signed in, can do this.
- **Your own never-mount list adds to the built-in list.** The built-in entries stay
  in the code: removing one is a change to maji-mcp, never a setting. Your list can
  only narrow what is mounted ([design/runner.md](design/runner.md)).
- **An item says what a path holds before you grant the path.** A grant exposes
  everything in the path, and the agent may not know what the path holds. Files
  named the way secrets are kept make the item high risk, with a sixth reason,
  *secrets in reach*. The reason goes by names, so the reason is only ever suspected.
- **An agent is told why a request was refused.** Until then every refusal reached
  the agent as "Error executing tool", without the reason or what to do instead.
- **An item shows what the app says it is,** labelled as unchecked. The part to trust
  is a gateway token for each agent, which makes each agent its own principal.
- **One notification will cover everything waiting,** sent only once an item has
  waited some minutes with no queue tab in view. Agreed, not built
  ([design/planned/queue.md](design/planned/queue.md)).

## 2026-11-09

- **What risk means.** The broker computes risk from what a request would do, never
  from what the agent says. High risk has named reasons, shown first, and takes your
  passkey even in an unlocked tab. Medium, the usual item, gets no tag. The queue is
  a gate on top of what the agent's own permission prompt already allowed, so the
  queue asks only what that prompt could not see
  ([design/control-plane.md](design/control-plane.md)).
- **High risk is a finding, never a verdict.** People will trust the label, and
  maji-mcp cannot judge risk in every context. Each reason therefore points at its
  evidence, and says whether the broker found the reason or only suspects it. No tag
  means none of the reasons was found, never that an item is safe.
- **"High risk" keeps its name, and text addressed to an AI stays one of the
  reasons.** "Exposure" and "scope" were weighed and dropped. "Possible injection"
  would claim too much for a reason that is only ever suspected.
- **A batch is for the easy, clear items.** An item with parts, an approval that can
  be remembered, or high risk is decided alone, and the client says why.
- **The shortest mount grant is the session.** Agents work in many small steps, and
  a script that mounts a path is usually followed by more scripts over the same
  path. A mount approved once would be asked for again at every step.
- **A grant covers a path, exactly as asked, never "a repository".** Profiles,
  captures and downloaded logs are not repositories. Git is something a script does
  on a mounted path. Grants then stay valid as other kinds of file arrive.
- **The runner sees your home directory, read-only, and resolves each path.** The
  Docker daemon resolves a bind source itself, and the broker and the control plane
  cannot see your files. The runner already holds the Docker socket, so the view
  gives the runner nothing more.
- **A script imports only listed libraries,** pinned from PyPI alone, each release at
  least 14 days old. An agent then writes Python against a documented interface, in
  place of building commands for another tool and parsing the output.
- **A live SQLite database is not read through a mount.** A test showed a held
  connection stuck far behind the writer ([design/runner.md](design/runner.md)).

## 2026-11-08

- **The queue replaces ediff for writes the broker applies.** Until the queue applies
  org writes, ediff stays on, because ediff is the only check on a write that reaches
  the bridge ([design/planned/writes.md](design/planned/writes.md)).
- **What you asked for is approval enough when the broker can check it.** A tick you
  asked for is applied at once. Content the requester wrote comes to the queue.
- **Repository writes land as a branch or a worktree,** once approved.
- **Every decision you make carries a signature; narrowing access needs none.** One
  touch covers a batch.
- **Deciding stays unlocked in a tab while you use the tab.** A passkey prompt on
  every decision is too much, with the password manager's prompt across the page
  from the decision's buttons. The defaults: ten minutes idle, with the passkey still
  asked for a lasting approval or a connection.
- **An approval can extend over time,** through patterns the broker computes, so
  later requests of the same shape use the approval.
- **Each part of a request is remembered separately.** A repository granted for later
  is covered in later requests, whatever other repositories those requests ask for.
- **Approving every part is the default.** Parts are for refusing some.
- **Content the requester wrote is never remembered, for now.** To revisit once there
  is a way to recognize edits of the same kind without semantic reasoning in the
  service.
- **Grants can be revoked at any time,** and are found by counts in groups and by a
  fuzzy search.
- **Bring-up is as simple as security allows.** `make up` opens the setup page with a
  one-time code in the link's fragment, so the first passkey takes one touch and
  nothing goes on the pasteboard. Recovery codes are shown once.
- **An instance id comes from the host's name,** fixed once made, because the id goes
  into signed hashes.
- **The control plane's internal socket is split by caller.** The broker and the
  gateway need different things and are trusted with different things.
- **The web client comes first,** and is used before the gateway's queue tools and
  changesets are built. The model is then checked against something you can look at
  and use.
- **The web client is Vue 3, TypeScript and Vite,** served by the control plane, with
  nothing fetched from another site. Accessible by design: screen reader, keyboard,
  colours chosen for the common kinds of colour blindness, and one type family in
  which `1`, `l`, `I`, `O` and `0` cannot be mistaken for each other.
- **Keys follow Emacs, not vi.** `j`, `k` and `x` stay as single-key shortcuts and
  can be turned off.
- **An agent learns of a decision by being told.** The agent ends its turn naming the
  item, and you tell the agent to go on. A status request answers at once and never
  waits.
- **An app keeps its own direct connections, writes included.** maji-mcp is another
  way to reach MCP servers, never a replacement.
- **Your words will travel with an item, as metadata.** The words clarify a request
  and approve nothing, because the agent could forge them.
- **The MCP SDK is 2.x,** ported as its own piece of work. Sentry's client stays on
  httpx, which checks TLS against certifi's bundle.
- **Starlette serves the REST API, and SQLite stores the run log, the queue and the
  decisions.** Both are minimal, which suits a small service. py_webauthn checks
  passkeys. Playwright stays a development dependency, for browser tests.

## 2026-11-07

- **Go.** Every answer was correct in both modes of the first measurement, and
  writing code cost 68% less overall. Skills are given priority over one-off scripts:
  a small write cost 26% more the first time, and about 80% less once the code
  existed ([research/measurements.md](research/measurements.md)).
- **The first build was judged done by cost.** One task cost what the baseline script
  mode did and wrote 17% more output. The output gap is recorded, to revisit.
- **`run_python` waits 60 seconds by default,** and a script can ask for up to 600.
  120 seconds is too long a default for an agent to wait.
- **Scripts keep results for each other and make independent calls at once**
  (`maji.save`, `maji.parallel`), to close a cost gap the second measurement found.
- **The gateway keeps MCP sessions,** which saved values need.
- **Decisions come at human speed.** Nothing that waits for a decision may time out
  on the scale of a script.
- **The runner reaches Docker through the socket's group,** never as root.
- **Git reaches repositories through mounts,** read-only.
- **Continuous integration waits** until the repository moves to its own home.

## Before the build

- **Existing MCP servers are used as they are.** maji-mcp adapts to a server and asks
  for no change. emacs-org-mcp is the first case: stubs parse its text output, and
  writes will go through its own tools, so its write guard and git commits still
  apply. Finer operations are skills composed from its existing tools.
- **The first build is read-only.** Write stubs are refused until changesets exist.
- **The first end-to-end run uses a REST service with nothing on your machine:**
  Sentry, read-only.
- **Deployment is option (a):** containers under your own Docker daemon. Agent-written
  code is always sandboxed, and the rest of the protection is proportionate
  ([design/deployment.md](design/deployment.md)).
- **Secrets and agent code never share a trust zone.** The code is the interface, so
  a boundary inside one process protects nothing. The broker holds credentials and
  runs no agent code, and the executor runs agent code and holds no credentials
  ([research/brainstorm-review.md](research/brainstorm-review.md)).
- **Permissions are enforced on each stub call at the broker.** Restricting which
  skills an agent can see restricts nothing, because the agent can write a script
  that calls any stub.
- **Each run is a fresh process in a fresh container.** Code loaded into a long-lived
  interpreter shares its globals, imports and memory. Python's subinterpreters are
  not a sandbox.
- **Measure before building.** Neither source article published a measurement anyone
  could reproduce ([research/evaluation.md](research/evaluation.md)).
- **Python 3.14, managed with uv,** with ruff, mypy, pytest and pre-commit.
  Annotations are evaluated lazily, so generated stubs can refer to types defined
  later.
