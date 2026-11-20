# Glossary

The terms the maji-mcp documents use, each with one meaning. A term marked *planned*
names something designed and not yet built.

## People and programs

| Term | Meaning |
|---|---|
| **you** | The person who runs an instance and makes every decision in the instance's queue. Text written for agents calls the same person "the user". |
| **agent** | An AI model at work in an app, such as Claude in Claude Code. An agent reaches maji-mcp only through the gateway's tools. |
| **app** | The program an agent works in. The app holds a gateway token and speaks MCP to the gateway. Claude Code and Claude Desktop are apps. |
| **principal** | The name the gateway gives a caller. The caller's gateway token alone decides the principal. Policy, rate limits, grants and kept skills belong to a principal. `claude-code` is the default principal. Every app that holds the same token is the same principal. |
| **gateway token** | The secret that identifies one principal to the gateway. The gateway stores only the SHA-256 hash of each gateway token. |
| **instance** | One running maji-mcp on one machine: the four containers, their volumes and the secrets directory. An instance has an instance id, made from the machine's host name when the instance is created. |
| **persona** | One identity's machines and data, such as work or home. Today an instance stands alone. Sharing within a persona and between personas is *planned* ([planned/sharing.md](planned/sharing.md)). |

## Components

A **component** is one of maji-mcp's own programs. A **service** is a system outside
maji-mcp that a stub reaches.

| Term | Meaning |
|---|---|
| **gateway** | The MCP server that apps connect to. The gateway identifies the principal and offers the tools ([gateway.md](gateway.md)). |
| **runner** | The component that starts one executor for each run. The runner is the only component that holds the Docker API ([runner.md](runner.md)). |
| **executor** | The container one run executes in. An executor has no network, no credentials and a read-only filesystem. The code in an executor is untrusted. |
| **broker** | The component that holds credentials and calls services. The broker checks every stub call and runs no agent code ([broker.md](broker.md)). |
| **control plane** | The component that holds the queue, the decisions, the grants and the passkeys, and serves the web client ([control-plane.md](control-plane.md)). |
| **web client** | The browser application you decide in. The control plane serves the web client. |
| **bridge** | A program on your machine that serves a stdio MCP server over HTTP on `127.0.0.1`, behind a token, for the broker. |
| **relay** | A program on your machine that serves the gateway over stdio, for an app that starts MCP servers as commands. Claude Desktop is such an app. |
| **headers helper** | `bin/maji-headers`, the script Claude Code runs each time Claude Code connects. The headers helper prints the gateway token and a new session id. |
| **service** | A system outside maji-mcp that the broker calls for scripts: Sentry's, Jira's or GitHub's REST API, or an MCP server such as emacs-org-mcp. |
| **service image** | `maji-service`, the Docker image the gateway, runner, broker and control plane containers start from. The image is named for the compose services it runs. |
| **runtime** | A language an executor can run. Python is the only runtime built. |
| **runtime image** | The Docker image an executor starts from. `maji-python-runtime` is the Python runtime image. |

## Running code

| Term | Meaning |
|---|---|
| **script** | Python source an agent sends to `run_python`. |
| **run** | One execution of one script in one executor. A run has a run id. |
| **run token** | A random secret the broker issues for one run. The run token is the only secret an executor holds. The broker finds the run's principal from the run token. The run token stops working when the run ends. |
| **stub** | A function a script calls and the broker carries out, named `<namespace>.<operation>`, such as `sentry.issues`. Each stub is classified as a read or a write when the stub's service is set up. |
| **namespace** | The stubs of one service, versioned together: `sentry`, `jira`, `github`, `org`, `maji`, `skills`. In a script, each namespace is a global. |
| **catalog** | The broker's list of every stub: parameters, documentation and handler. |
| **policy** | The broker's file that names, for each principal, the stubs the principal may call and the principal's rate limit (`config/policy.json`). |
| **session** | One app's stretch of work with maji-mcp. A session is what a grant "for this session" lasts for, and what saved values belong to. [gateway.md](gateway.md) says when a session begins and ends. |
| **session key** | The SHA-256 the gateway derives from the principal and the session's id. The broker and the control plane know a session only by the session key. |
| **session name** | Three words computed from the session key, such as `laughing-sparky-daisy`. The session name is a label for people. Grants and decisions stay bound to the session key. |
| **saved value** | A JSON value a script stores with `maji.save` for later scripts of the same session. The broker holds saved values in memory. |
| **run log** | The broker's database of runs, stub calls and the runtime libraries each run used. The run log records names and outcomes, never arguments or results. |
| **runtime library** | A third-party Python library a script can import, such as pandas, installed in the runtime image. Not the skills' **library**. |
| **mount** | A path under your home directory, bound read-only into an executor at `/mnt/<name>`. |
| **cover** | What hides a path inside a mount. A covered file reads as an empty file, and a covered folder lists as an empty folder. The runner covers files named like secrets and entries of your never-mount list. |
| **mount root** | The directory a mounted path must be inside: your home directory. |
| **never-mount list** | Your own file of paths that are never mounted, whatever is granted. A folder that holds a listed path is mounted with the listed path covered. |

## Skills

| Term | Meaning |
|---|---|
| **skill** | A Python function kept in the library for reuse. A script calls a skill as `skills.<name>(...)`. |
| **library** | The directory tree that holds every skill, on a volume only the broker mounts. |
| **version** | One saved state of a skill: the code, the manifest and `SKILL.md`. A version is named by the first 16 hex characters of the hash of the code and the manifest together. |
| **manifest** | `manifest.json` in a version: the function, the signature, the summary, and the stubs, skills and modules the code uses. |
| **kept version** | A version an agent saved with `save_skill`. A kept version runs for the principal that kept it. Nobody has reviewed a kept version. |
| **promoted version** | The one version of a skill you approved in the queue. The promoted version runs for every principal whose policy allows `skills.<name>`. |
| **demoted version** | A promoted version you took back. A demoted version runs for no principal. |
| **publisher** | The namespace an instance saves skills under. The default publisher is `me`. |
| **pack** | A named group of skills under one publisher. Every skill is in the pack `local` today. Packs shared between personas are *planned*. |

Older documents call a kept version a *candidate* and a demoted version *revoked*.

## The queue

| Term | Meaning |
|---|---|
| **queue** | The items waiting for your decision, together with the history of decided items. |
| **item** | One request that needs your decision. |
| **kind** | The type of an item. The kind fixes the actions the item offers. |
| **requester** | Who raised an item: the principal, the session, and the name and version the app reported. |
| **facts** | What the broker computed about a request: the paths, the diff, the stubs a skill calls. |
| **claim** | The requester's own explanation. Nothing checks a claim, and a client labels a claim as the requester's words. |
| **part** | One piece of an item that you approve or refuse separately, such as each path of a mount request. |
| **risk** | What the broker found a request would do: `low`, `medium` or `high`. |
| **risk reason** | One of six named reasons an item is high risk. Each risk reason is either **found**, from what the request does, or **suspected**, from a guess. |
| **action** | One thing an item lets you do: Grant, Deny, Promote and sign, Demote. |
| **decision** | Your signed record of one action on one item. Decisions are append-only. |
| **choice** | What a client sends to make one decision: the item, the content hash, the action, and any parts, note, answer or remembered patterns. |
| **content hash** | The hash of an item's content. A choice carries the content hash of the item as the client showed the item. |
| **reconsideration** | A decision that takes back an earlier decision on the same item, and names the earlier decision in `supersedes`. Demoting a skill is a reconsideration. |
| **outcome** | What applying a decision did, as the broker reports: applied, conflict or failed. |
| **decision feed** | The ordered list of decisions the broker reads from the control plane and applies. |
| **grant** | A recorded permission for one principal, made when you approve an item and choose to remember the approval. A grant lasts for a run, a session, until a time, or until revoked. A request a grant covers raises no item. |
| **pattern** | The broker's description of what a remembered approval would cover, offered on an item. Approving with a pattern makes a grant. |
| **refusal** | Your decision to deny a part. A refused path stays refused for the session until you let the agent ask again. |

## Signing in and signing

| Term | Meaning |
|---|---|
| **passkey** | A WebAuthn credential on your device. Every decision is signed with a passkey, or with a tab key a passkey certified. |
| **assertion** | One signature by a passkey over a challenge the control plane derives from the decision. |
| **setup code** | A one-time code that registers the first passkey. A setup code works once, for ten minutes. |
| **recovery code** | One of ten one-time codes shown with the first passkey. A recovery code adds one passkey. |
| **sign-in** | Your browser's authenticated use of the control plane, held in a cookie. The documents reserve "session" for an agent's session. |
| **unlocked tab** | A browser tab whose tab key a passkey has certified. An unlocked tab signs decisions with no passkey prompt until the tab goes ten minutes unused. |
| **tab key** | An ECDSA key pair a browser tab makes and cannot export. |
| **client token** | A token for a command-line client of the control plane. A client token reads the queue and narrows access. A client token never decides. |
| **secrets directory** | `~/.config/maji-mcp/secrets`, the directory of files the compose stack reads secrets from. `make setup` creates the secrets directory. The secrets directory is outside every project, in a hidden folder the runner never mounts. |

## Planned terms

| Term | Meaning |
|---|---|
| **changeset** | A proposed write with a precondition, applied only after a decision ([planned/writes.md](planned/writes.md)). |
| **precondition** | What must still be true of a write's target when the write is applied. |
| **connection** | maji-mcp's own credential for one service, which you paste into the web app and only the broker keeps ([connections.md](connections.md)). |
| **connector** | What tells the broker how to reach one service that has no built-in stubs: a REST definition, a database definition, an MCP server's address or command, or code of your own. A connector is one namespace ([planned/adding-connectors.md](planned/adding-connectors.md)). |
| **REST definition** | A connector written as JSON: a REST API's address, how the key is sent, and the requests scripts may make. |
| **database definition** | A connector written as JSON: a database's address, user and limits, and any tunnel or credential helper. |
| **connector file** | `maji-connector.toml`, at the top of a folder or repository of code of your own: the namespace, the command, and each tool's classification. Not a skill's **manifest**. |
| **fetcher** | A connector that copies data with a credential into a cache that executors mount read-only. A fetcher runs in a container of its own, with a token narrowed to its job ([planned/adding-connectors.md](planned/adding-connectors.md)). |
| **egress proxy** | The container a fetcher's requests go through, which allows each fetch only the hosts its connector names ([planned/connectors.md](planned/connectors.md)). |
| **launcher** | The program on your machine that runs the commands you approved for connectors: MCP servers started as commands, code of your own, tunnels and credential helpers. |
| **credential helper** | A command on your machine that prints a short-lived password for a connector, such as an RDS IAM token. |
| **tunnel** | A command on your machine that forwards a local port to a service the broker can't reach directly, such as `kubectl port-forward`. |
| **Claude Code skill** | Instructions Claude Code loads for one kind of task, from a `SKILL.md`. Not a maji-mcp **skill**. |
| **policy rule** | A rule in the control plane that decides a low-risk kind of item without you ([planned/writes.md](planned/writes.md)). A policy rule is separate from the broker's policy file. |
| **advisor** | A second agent that annotates items and decides nothing ([planned/queue.md](planned/queue.md)). |
| **analysis tool** | A tool outside maji-mcp, such as Corridor, that judges the risk of a waiting item and returns findings. An analysis tool decides nothing ([planned/queue.md](planned/queue.md)). |
| **trust set** | A persona's registered public keys, all of them yours ([planned/sharing.md](planned/sharing.md)). |
| **run policy** | One instance's rules for which skill versions may run there ([planned/sharing.md](planned/sharing.md)). |
