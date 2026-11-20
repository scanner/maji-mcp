# maji-mcp brainstorm: review

**Status:** a record, written at the start of the design (early November 2026) and
kept as written. The review set the trust zones the design still has. Later
decisions changed the details: keeping a skill needs no review, and promotion is for
sharing or for writes ([../design/skills.md](../design/skills.md)).

## What holds up
- **Owning and hosting the executor yourself.** Nothing in the concept needs Cloudflare or Anthropic infrastructure. Their designs are one way to get the boundary, not the only way.
- **The skill store as a datastore.** A store can be versioned, backed up, measured and analysed. Usage metrics will tell you which one-shots deserve promotion, so they're useful from the start, not just nice to have.
- **Git as the versioning layer.** It fits better than you may have noticed (see 4).
- **Credentials from an external store** such as Vault, 1Password, or a secret mount.
- **Sentry as the motivating case.** It sits right on the line. When the event and its context make the cause obvious, a direct call is enough. Correlating across stacks, releases and fingerprints is counting and grouping, which is what code is for.

## 1. Secrets and agent code cannot share a trust zone
This is the flaw that matters most. The plan is that secrets sit inside the container and the exported interface stops the agent from reading them. But **the agent writes arbitrary Python, so the code is the interface.** Code running as the same user in the same container can read:
- environment variables, including the parent's, via `/proc/<pid>/environ`
- mounted secret files
- the 1Password or Vault token used to fetch everything else

There's a second leak specific to the plan. A `multiprocessing` worker started with `fork` gets a copy of the parent's memory, so whatever credentials the parent has loaded are already in the worker.

The fix is the same split Cloudflare makes, but in your own terms. Use two processes with different privileges:

- **Broker:** holds the credentials, the MCP client connections and the API clients. It runs no agent code. It exposes stubs over a socket (RPC).
- **Executor:** runs agent code with no credentials, no network except the broker's socket, and its own user ID or its own container.

"Bindings" just means the executor's stubs are RPC clients to the broker. Every boundary you care about — secrets, RBAC, approval for writes, rate limits — then sits in the broker, which you wrote and trust.

## 2. Hot-loading modules into a long-lived process gives you no isolation
Code loaded into a running interpreter shares its globals, its imports and its memory. A runaway loop, a leak, or a script that patches a module breaks every later run. Run each execution in a **fresh process**: spawn a new one, don't fork. If you use a pool, set `maxtasksperchild=1`. Give each run a CPU time limit, a memory limit, a wall-clock timeout and a cap on output size. Python starts in tens of milliseconds, so this costs little. Parallelism then comes from running several of these processes at once, which multiprocessing or a plain subprocess pool handles fine.

## 3. Skills are not a permission boundary, so RBAC belongs at the broker
Restricting which skills an agent can see restricts nothing, because the agent can write a script that calls any stub directly. Enforce RBAC on **each stub call at the broker**, keyed to the connection's identity: "this principal may read Sentry for project X and may not write to org tasks." What you can slice finely is the broker's per-call policy, nothing else.

The same applies to agents sharing one container. Either they count as one principal, or each session gets its own user ID and working directory, so one agent's scratch files aren't readable by another's scripts.

## 4. Promotion is a supply-chain path, and prompt injection can use it
Sentry events contain strings that other people control: request bodies, exception messages, user agents. An agent that reads them can be steered. If the agent can also **promote** code into the shared library, an injected instruction can become a skill that later runs under another agent's permissions, on another machine, after replication.

So promotion requires **human review**, recorded as your signature:
- One-shot scripts (drafts) stay in their session's workspace and are never replicated as trusted code.
- Promotion is a decision in the control-plane queue. You review the code and its `SKILL.md`, and sign it with one of your persona's keys (see [control-plane.md](../design/control-plane.md)).
- Instances run a promoted skill only after checking that signature, wherever its files came from (see [planned/sharing.md](../design/planned/sharing.md)).

Because trust comes from the signature, replication can use whatever transport suits it (section 5).

## 5. Replication is mostly solved already, provided you keep three stores separate
The brainstorm treats "the datastore" as one thing. It is really three, with different consistency needs:

| Store | Contents | Replication |
|---|---|---|
| Skill library | candidate and promoted skills: source + manifest + `SKILL.md` | file sync (Syncthing, Resilio) within one persona: snapshot directories plus a `CURRENT` pointer. Git between personas: packs pinned to a commit or tag. Trust comes from signatures either way; see [planned/sharing.md](../design/planned/sharing.md) |
| Execution log / metrics | every run: script, caller, stubs called, duration, result size | append-only, each instance writes its own and ships it to one sink |
| Session workspace | intermediate files, a one-shot's state | ephemeral, local, never replicated |

Don't let several instances share one mounted volume and run git writes on it: concurrent git operations on one working tree will corrupt it. Give each instance its own clone, so the instances stay independent and still look like one service, which is the property you asked for. With git as the remote, backup is just another clone.

## 6. Smaller points
- **Rate limits and quotas belong in the broker.** A loop in agent code can make 10,000 Sentry API calls in a minute. With direct tool calls, the round trips through the model used to throttle that by accident.
- **Stubs should return structured data** (dataclasses or dicts), not the formatted text your MCP tools return for the model to read. Code needs fields to group by.
- **Wrapping a third-party MCP and wrapping its REST API are different jobs.** Wrapping the MCP gets you its invariants. Wrapping the API gets you speed and full fields, but you write the invariants yourself. Sentry's REST API is probably the better source for the correlation work.
- **Parallelism within one run is a different thing from parallelism across agents.** A script that fetches 200 events should batch or run concurrently through the broker, not loop serially. A stub that takes a list of ids helps with that.

## Architecture

The trust zones from sections 1–5 are drawn in [architecture.md](../design/architecture.md), together with the control plane, the runner and the live Emacs. There are two diagrams there: request flow and trust zones, and the skill lifecycle and replication.

## A first slice that tests the hard parts
The first build is stage 1 of [the roadmap](../roadmap.md): broker, runner and executor on one machine, as containers under your Docker daemon, with Python stubs over structured outputs and a run log. It follows stage 0, which measures whether the idea saves tokens on real tasks.

Replication, the external vault, containers, the control plane and multi-tenant RBAC all sit on top of this core without changing it, provided the broker/executor boundary is right from day one.

## Follow-on: sharing skills
Exporting skill packs, importing them, and keeping them up to date is a later
feature. [planned/sharing.md](../design/planned/sharing.md) lists the decisions the first stages make
now so that feature stays possible: a namespaced library, a manifest per skill, stub APIs
with stable versions, and a library made of several pinned sources.
