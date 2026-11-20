# The control plane

The control plane holds every decision that needs you, as a queue. This document covers
items, risk, deciding, grants, signing with a passkey, and the control plane's
interfaces.

**Status:** this document describes what is built. Two kinds of request reach the queue
today: a request to mount paths, and a request to promote a skill. The control plane's
data model and API already define more kinds of item, whose flows are planned
([planned/writes.md](planned/writes.md), [planned/connections.md](planned/connections.md),
[planned/queue.md](planned/queue.md)). Code: `src/maji_mcp/control/`, `web/`,
`api/openapi.yaml`.

## Why a queue

One session of an agent can produce dozens of requests. A prompt for each request
interrupts you, asks for an answer before you have the context, and blocks the agent
until you answer. A queue turns the requests into a list you work through at your own
pace, with the evidence for each request in front of you. The agent carries on with
other work while a request waits.

Decisions come at human speed: minutes, or hours while you are away. Nothing that
waits for a decision is allowed to time out on the scale of a script. `run_python`
returns at once, naming the item, and the agent runs the request again after you have
decided.

The queue is a gate on top of the agent's own permission prompt. The agent's app has
already allowed the call "run this code". The queue asks about what that prompt could
not see: what the code would reach.

## What a queue item is

An item is one request that needs your decision.

| Field | Meaning |
|---|---|
| `id` | the instance id, a hyphen, and a ULID, which sorts by time |
| `kind` | the type of request, which fixes the actions |
| `title`, `subject` | what is asked, in a line |
| `requester` | the principal, the session, the run, and the name and version the app reported |
| `risk`, `risk_reasons` | what the broker found the request would do |
| `facts` | what the broker computed about the request |
| `diff` | a change to read, such as a skill's code against the promoted version |
| `claim` | the requester's own explanation |
| `parts` | pieces you approve or refuse separately, such as each path |
| `patterns` | what a remembered approval could cover |
| `actions` | what you can do now |
| `expires` | when an undecided item lapses and goes back to the requester |
| `session_name` | the three-word name of the requester's session |

**Facts and claim are kept apart.** A request comes from an agent that may have read
injected text. The broker computes the facts. The requester writes the claim. A client
shows the facts first and labels the claim as the requester's words, so the
requester's words never frame a request before you have seen the request.

### Ahead, now, behind

The queue is a timeline you move along in both directions.

- **Ahead** is what waits: items an agent is blocked on first, then by thread, then
  by priority, then by age. A thread groups the items of one session's goal.
- **Now** is the item in front of you: the facts, the diff, the requester's claim and
  the actions.
- **Behind** is every item already closed, newest first: what was decided, by whom,
  when, how the decision was signed, and your note.

### Kinds

| Kind | Actions | Raised today |
|---|---|---|
| `grant` | Grant, Deny | yes: a request to mount paths ([runner.md](runner.md)) |
| `promote` | Promote and sign, Request changes, Reject; then Demote | yes: `promote_skill` ([skills.md](skills.md)) |
| `changeset` | Approve, Return with note, Reject | planned |
| `question` | Answer, Decline to answer | planned |
| `connect` | Connect, Deny; then Disconnect | yes: a stub whose service has no key, or the Connections page ([connections.md](connections.md)) |
| `policy_change` | Accept, Reject | planned |
| `server_registration` | Approve, Reject | planned |
| `run_candidate` | Allow once, Allow on this machine, Deny | planned |
| `pack_import`, `pack_export` | Accept and sign or Approve export, Reject | planned |

The control plane stores, shows and decides every kind. `make demo` shows several
kinds with sample data. Only the first two kinds have a broker flow behind them.

## What makes an item high risk

The broker computes an item's risk from what the request would do, never from what the
agent says.

- **Medium** is the queue's usual item, and carries no tag.
- **High** is one of six reasons. A high-risk item names each of its reasons, with
  what the broker found.
- **Low** is for requests a policy rule could decide without you. Policy rules are
  planned.

| Reason | Meaning | Raised today |
|---|---|---|
| `trust_changes` | The decision changes what later requests may do without you: promoting a skill that writes, or a skill whose reach cannot all be read from its code. | yes |
| `secrets_in_reach` | A path to mount is a file named the way secrets are kept, asked for by name. A mount of a folder leaves such files out, so a folder raises no such reason. | yes |
| `data_leaves` | Data leaves your machines: an export, or a key sent to a new host. | planned |
| `your_words` | A change removes or replaces content you wrote. | planned |
| `addressed_to_ai` | The request carries text worded as an instruction to an agent. | planned |
| `beyond_the_task` | The request reaches something the session's goal does not name. | planned |

A high-risk item says why before anything else, takes your passkey every time, even
in an unlocked tab, and is decided alone, never in a batch.

**High risk is a finding.** The broker sees what a request would do inside maji-mcp.
The broker does not know what the request means to you.

- **Each reason points at evidence on the item:** the fact, the diff line or the part
  the reason rests on.
- **Each reason is found or suspected.** A reason is *found* when the broker
  establishes the reason from what the request does. A reason is *suspected* when the
  reason rests on a guess. `secrets_in_reach` goes by file names,
  `addressed_to_ai` rests on a detector, and `beyond_the_task` rests on a judgment of
  the session's goal. These three reasons are only ever suspected.
- **No tag does not mean safe.** No tag means the broker found none of the six
  reasons. No client calls an item low risk, safe or checked.

**High risk has to stay rare to mean anything.** If every item were high risk, the
passkey touch would become a reflex and the label would go unread.

## Deciding

- **An item offers the actions the control plane lists for the item's kind.**
- **Some actions need something.** Request changes needs a note. Answer needs an
  answer.
- **A request with parts lists every part, each approved unless you refuse the part.**
  A mount request has one part for each path.
- **A choice carries the content hash of the item as the client showed the item.**
  The control plane decides an item only while the item still waits and the item's
  content hash is still the hash in the choice. Otherwise nothing is decided, and the
  client shows the item as the item is now.
- **A batch lands whole or not at all.** One signature covers every choice in a
  batch. A batch is for items approved as they stand: an item with parts, an item
  whose approval can be remembered, and a high-risk item are each decided alone.
- **An undecided item expires** and goes back to the requester. The control plane
  sweeps for expired items every 30 seconds.

### History is append-only

Every decision is kept: what was decided, by whom, when, how the decision was signed,
and your note. Database triggers refuse any update or delete of a decision.

Changing your mind is a new decision on top of the old one, never an edit. A decided
item offers the actions that reconsider the item's latest decision.

- **A promotion offers Demote** while the promotion is the one in effect for the
  skill ([skills.md](skills.md)).
- **A reconsideration names the decision it takes back,** in `supersedes`, and
  carries a note at most. A reconsideration only narrows what agents can do, so an
  unlocked tab signs a reconsideration whatever the item's risk.
- **The control plane refuses a reconsideration that carries anything else,** such as
  a remembered approval, and refuses one whose `supersedes` is no longer the item's
  latest decision.

### Outcomes

The control plane records decisions. The broker enforces decisions. The broker reads
the decision feed, applies each decision, and reports an outcome: *applied*,
*conflict* when the thing decided on changed in the meantime, or *failed*. A decided
item shows whether the decision has been applied.

## Remembering an approval: grants

An approval can be remembered, so later requests of the same shape need no decision.

- **The broker offers patterns on an item,** computed from the item's facts. A mount
  request has one pattern for each path.
- **Approving with a pattern makes a grant** for the requester's principal, signed
  with the same signature as the decision.
- **Each part is remembered separately.** A repository granted for later is covered
  in a later request, whatever else the later request asks for. Only the paths no
  grant covers come to the queue.
- **One choice sets how long every part is remembered,** and each part's own choice
  still changes that part alone.
- **A refusal never makes a grant.**

| A grant lasts | Meaning | Signed by |
|---|---|---|
| for the run | until the run that asked ends | an unlocked tab |
| for the session | until the requester's session ends | an unlocked tab |
| for the session, until a time | until the session ends or the time comes, whichever is first; the web client offers 8 hours | an unlocked tab |
| until a time | every session of the principal, until the time | the passkey |
| always | every session of the principal, until you revoke the grant | the passkey |

**A mount is remembered for the session at least.** `run_python` returns as soon as
the request is raised, and the agent runs the script again after you decide. A mount
approved for one run would cover nothing by then. The control plane refuses an
approval that keeps an approved path for less than the session. A request from an app
with no session is granted for a time instead, 8 hours by default.

**A grant on a folder covers everything in the folder,** apart from what a mount of
the folder leaves out: files named like secrets, and entries of your never-mount list
([runner.md](runner.md)). A file named like a secret needs a grant on exactly that
file. A refusal covers exactly the path refused.

**You can let an agent ask again for a path you refused.** A refused path stays
refused for the session, so an agent that retries does not put the same path to you
again. Once you have told the agent more, the decided item offers "Let it ask again".
The action grants nothing, is recorded beside the refusal, and can be taken only from
a signed-in browser.

**Grants can be revoked at any time.** The Grants page finds a grant by any fragment
of what the grant covers or who holds it, with a fuzzy search in the manner of fzf.
Grants are revoked one at a time, or a group at once: grouped by what the grants
cover, by principal, by access, by duration, or by the session that holds them. Only
a grant that lasts for a session is held by the session. A grant made in a session
that lasts longer is never revoked with the session's grants.

## Sessions on items and grants

An item and a grant show the requester's session by the session name, such as
`laughing-sparky-daisy`, with the first 8 characters of the session key beside the
name ([gateway.md](gateway.md)). The session name is a label. A grant stays bound to
the session key. When a run waits on an item, the agent is told the session name the
queue shows, so the agent can tell you which item is the agent's own.

## Signing: passkeys

Every decision is signed by you. A token never decides.

**A passkey assertion is bound to one decision.** The control plane puts each choice
into canonical JSON (RFC 8785) and hashes it. The WebAuthn challenge is built from the
hashes of exactly the choices you made, so the assertion covers those choices and no
others. One assertion over several hashes signs a batch with one touch. The control
plane verifies every assertion against its exact origin and relying-party ID, with
the user verified (`src/maji_mcp/control/passkeys.py`).

**An unlocked tab signs without a prompt.** A passkey prompt on every decision is too
much for a queue worked through in one sitting. Your first decision's passkey touch
also certifies a tab key: an ECDSA P-256 key pair the browser tab makes with
WebCrypto, cannot export, and keeps only in memory. The tab then signs each decision's
challenge with the tab key, until the tab goes ten minutes unused, is locked, reloads
or signs out. Each decision records the tab key and the passkey's assertion over the
tab key, so the decision can be verified again anywhere. The Passkeys page can turn
unlocking off.

**Some decisions take the passkey even in an unlocked tab:**

- a decision on a high-risk item
- an approval remembered until a time, or always
- connecting a service

A reconsideration never needs the passkey in an unlocked tab.

**Passkeys are managed in the web client.**

- **The first passkey** is registered with a setup code. `make up` opens the setup
  page with the code in the link's fragment, which a browser never sends to a server.
  The code works once, for ten minutes.
- **Ten recovery codes** are shown once, with the first passkey. Each recovery code
  adds one passkey, once.
- **A later passkey** is added from a sign-in made within the last five minutes, or
  with a recovery code.
- **The last passkey cannot be removed.**

A passkey is bound to a relying-party ID, which is a domain. The control plane is
therefore served at `localhost`, never at `127.0.0.1`.

## Who may do what

| Caller | Authenticates with | May |
|---|---|---|
| You, in the web client | a passkey sign-in, held in a cookie | read everything; narrow access, such as revoking a grant; decide, with a signature |
| A command-line client | a client token | read the queue; narrow access; never decide |
| The broker | the broker's socket, in a volume only the broker and the control plane mount | raise items with facts and patterns; ask whether a grant covers a request; read decisions; report outcomes; withdraw an item the broker raised; report the services it can call with a key; ask for a service's Connect item; take the key pasted with a Connect decision, once |
| The gateway | the gateway's socket | submit an agent's question; read or withdraw the agent's own items. No gateway tool uses the socket yet. |

Neither internal socket has a method that decides. A decision enters only through the
REST API, with a signature.

## Interfaces

### The REST API

`api/openapi.yaml` is the contract. Every client is built on the contract, and the web
client's TypeScript types are generated from the contract on every build. The control
plane serves the API under `/api/v0` on `localhost:8767`.

Every request passes three checks before a route sees the request
(`src/maji_mcp/control/api.py`):

1. **Host.** The `Host` header must be the configured origin's host and port, as a
   guard against DNS rebinding.
2. **Who is asking.** A bearer client token, or the sign-in cookie. The control plane
   stores only the hash of either.
3. **Origin.** A request that is not a GET, and is not made with a bearer token, must
   carry an `Origin` equal to the control plane's own. With the cookie's
   `SameSite=Strict`, the check keeps another site from acting through your browser.

| Routes | Status |
|---|---|
| items, decisions, unlocks, grants, passkeys, sign-in, events, health, reading policy, listing connections and asking to connect a service | built |
| `/resources`, `/policy/proposals`, `DELETE /connections/{service}`, `/oauth/callback` | in the contract, not built; a test lists them so a route cannot go missing unnoticed |

**A key pasted into a Connect item never reaches the database.** It comes beside the
decision in `POST /decisions`, must match the fingerprint the decision signs, and is
held in memory until the broker takes it (`src/maji_mcp/control/handoff.py`,
[connections.md](connections.md)).

### The event stream

`GET /events` is a server-sent event stream that carries ids only. A client resumes
from `Last-Event-ID` for a day. After a day, the stream says `resync`, and the client
lists the items again.

### The internal sockets

Two Unix sockets carry JSON-RPC, one for the broker and one for the gateway
(`src/maji_mcp/control/internal.py`). The table above lists what each caller may do.

### The web client

The web client is a Vue 3 and TypeScript application in `web/`, built with Vite and
served by the control plane. The web client loads nothing from another site, fonts
included. [client-rules.md](client-rules.md) states the rules every client follows:
what a client shows, how deciding works, and how a client is used from the keyboard
and with a screen reader.

The control plane also serves one page of its own, the setup page
(`src/maji_mcp/control/pages/`), which registers the first passkey before the web
client can be used.

## The database

One SQLite file for each instance, in WAL mode (`src/maji_mcp/control/store.py`). The
rules that make a decision trustworthy are kept by the database itself, so no code
path can skip them:

- **Decisions, outcomes, grants, revocations and events are append-only.**
- **An item is decided once,** in a statement that succeeds only while the item's
  content hash is still the hash decided on.
- **A batch of decisions lands in one transaction.**
- **Challenges, setup codes and recovery codes are used once.**
- **Secrets are stored as hashes:** client tokens, sign-in cookies, setup codes and
  recovery codes.
- **The database belongs to one instance.** The store refuses to open a database as
  any other instance.

**Backups.** `make backup` copies the database with SQLite's online backup while the
control plane runs. The copy goes into a directory only you can read, outside the
repository and any synced folder, because the queue's facts and diffs can include
your data. `make restore FROM=<file>` stops the control plane, checks the file's
integrity and that the file belongs to this instance, and puts the file in place.

## The demo queue

`make demo` runs a separate control plane on `http://localhost:8768`, with a database
whose instance is `demo`, seeded with sample items of every kind the web client
shows. Seeding refuses any database whose instance is not `demo`. Nothing in the demo
is ever applied: the demo has no broker behind it.

## What the control plane does not hold

- **No signing keys.** Signatures come from your passkeys and from tab keys.
- **No credentials for services.** The broker holds those.
- **No way for a token to decide.**

In the default deployment your user owns the Docker daemon, so a program running as
you could reach the control plane's database with `docker exec`
([security.md](security.md)).

## Planned

- **Writes as changesets,** policy rules, and coalescing
  ([planned/writes.md](planned/writes.md)).
- **Connections** to services, set up through the queue
  ([planned/connections.md](planned/connections.md)).
- **Questions, the advisor, your words on an item, notifications, and help judging
  risk** ([planned/queue.md](planned/queue.md)).
- **More clients:** a macOS menu bar app, an Emacs buffer, a command line
  ([planned/clients.md](planned/clients.md)).
- **One queue across your machines:** items and decisions replicated within a persona
  ([planned/sharing.md](planned/sharing.md)).
