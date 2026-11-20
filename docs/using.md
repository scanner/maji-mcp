# Using maji-mcp

How to start an instance, connect an agent's app, work the queue, and find out what
went wrong when something does. [design/glossary.md](design/glossary.md) defines the
terms.

`make help`, or `make` on its own, lists every command. Run every command from the
root of the checkout.

## What you need

- **Docker on a Mac:** Rancher Desktop, Docker Desktop or Colima. The commands use
  the Docker CLI's current context, the one `docker context show` names. Set
  `DOCKER_HOST` for another daemon. Only Rancher Desktop is tested, and Linux does
  not work yet ([../README.md](../README.md), "Supported platforms").
- **uv,** which installs Python 3.14 and the dependencies.
- **A browser with passkeys,** such as Chrome with Touch ID.
- **Claude Code, Claude Desktop, or both.**
- **Node 22,** only for `make demo` and for developing the web client. The service
  image builds the web client for itself.

## Start an instance

```sh
make up
```

`make up` does four things:

1. builds the two images
2. creates each missing file in `~/.config/maji-mcp/secrets`, and never replaces a file
3. starts the four containers
4. opens the control plane's setup page, while no passkey is registered

**Register your first passkey** on the setup page, at `http://localhost:8767/setup`.
The link carries a one-time code that works once, for ten minutes. `make up` again
gives a new link. Run without a terminal, `make up` prints the link.

- Save the ten recovery codes the page shows, in your password manager. Each recovery
  code adds one passkey, once, so keep the codes as safe as a passkey.
- Register as soon as the stack is up. Until a passkey is registered, a program
  running as you could use the link first. Afterwards, check on the Passkeys page
  (`http://localhost:8767/passkeys`) that the only passkey listed is yours.

## Connect an app

**Claude Code:**

```sh
make connect-claude-code
```

`make connect-claude-code` adds the gateway to Claude Code as a server named `maji`,
for every project. `SCOPE=local` adds it for this project alone. Claude Code runs
`bin/maji-headers` each time Claude Code connects, so the gateway token is read from
the secrets directory and never copied into Claude Code's settings. For a server added with
`SCOPE=local`, Claude Code runs the helper only once you have accepted the project
folder's trust dialog.

**Claude Desktop:**

```sh
make connect-claude-desktop     # with Claude Desktop closed
```

`make connect-claude-desktop` adds `maji` to Claude Desktop's settings file, keeping
every other setting. Quit Claude Desktop first, because Claude Desktop keeps its own
settings in the same file. One run of Claude Desktop is one session, shared by every
chat: a grant for the session holds for every chat, until Claude Desktop quits or the
session goes an hour without a script.

Both apps connect as the principal `claude-code`. `PRINCIPAL=name` on `make setup`,
`make connect-claude-code` and `make connect-claude-desktop` uses another principal,
which also needs an entry in `config/policy.json`.

**Another app.** Each app with a command has a target named for the app,
`connect-<app>`. Any other app that speaks MCP over HTTP connects by hand:

- the server's URL is `http://127.0.0.1:8765/mcp`
- the app sends `Authorization: Bearer <token>`, with the token in
  `~/.config/maji-mcp/secrets/<principal>.token`
- an app on MCP's 2026-07-28 revision, which has no sessions, also sends an
  `X-Maji-Session` header of 16 to 128 letters and digits, new for each stretch of
  work, so grants "for this session" and saved values have a session to belong to

Give each app its own principal, with `make token PRINCIPAL=name` and an entry in
`config/policy.json`, when the app should be treated differently from the others.

A principal's name is 1 to 64 letters, digits, hyphens and underscores, and starts
with a letter or a digit, because the name is also the name of the principal's token
file. `emacs-org` and `control-cli` are not available: `emacs-org.token` is the
bridge's token, and `control-cli.token` is the command-line client's.

## Give scripts something to call

**Sentry.** Open **Connections** in the web app and choose Connect beside Sentry, or
wait for an agent's script to need Sentry, which puts a "Connect Sentry" item in your
queue. In the item, paste a Sentry user auth token that can read your organization,
its projects and their issues and events (`org:read`, `project:read`, `event:read`;
the item links to where you make one), give your organization's slug, and sign with
your passkey. Only the broker keeps the token, and sends it only to `SENTRY_URL`
([design/connections.md](design/connections.md)). To change the token, choose Replace
the key; to take it back, open the item that connected Sentry and choose Disconnect.

**Jira,** the same way, with a scoped Atlassian API token. On your Atlassian account's
API tokens page, create a token with scopes, for Jira, with `read:jira-work`. Give
your site's name, as in `<site>.atlassian.net`, and the email of your Atlassian
account. The token lasts a year at most; connect Jira again when it expires.

**GitHub,** the same way, with a fine-grained personal access token. Choose the
repositories, and give read access to Metadata, Contents, Pull requests and Issues. An
organization may have to approve the token before it reads the organization's
repositories. For GitHub Enterprise Server, set `GITHUB_API_URL` before `make up`.

**[emacs-org-mcp](https://github.com/scanner/emacs-org-mcp),** or another stdio MCP
server that acts as you. Clone emacs-org-mcp to `~/src/emacs-org-mcp`, or name
another checkout with `EMACS_ORG_MCP=`:

```sh
make bridge          # starts it in the background, and restarts the broker
make bridge-stop     # stops it
```

`make bridge` serves emacs-org-mcp on `127.0.0.1:8766`, behind a token, and returns
once the bridge answers. The broker connects to its servers as the broker starts, so
`make bridge` then restarts the broker when the stack is up. `make status` says
whether the bridge is running, and `make bridge-logs` shows what the bridge wrote.
`make bridge-fg` runs the bridge in the foreground, to watch it.

The bridge runs until you stop it or log out. Start it again after a restart of your
machine.

`EMACS_ORG_MCP=` names the checkout, and `ORG_DIR=` another org directory, such as a
copy. `config/servers.json` lists the tools the broker exposes.

**`config/policy.json`** names the stubs each principal may call. After an edit, run
`make restart SERVICE=broker`.

## The queue

The queue is at `http://localhost:8767/queue`. Sign in with your passkey, and decide
what is waiting, one item or a batch at a time.

- **Your first decision's passkey touch unlocks deciding in the tab,** until the tab
  goes ten minutes unused. A decision on a high-risk item, or an approval remembered
  beyond the session, still asks for the passkey. The Passkeys page can turn
  unlocking off.
- **Three kinds of request reach the queue today:** a request to mount paths for a
  script, a request to promote a skill, and a request to connect a service with a
  key.
- **The Grants page** lists what you have granted, and revokes grants one at a time
  or a group at once.

## Granting a project

An agent names the folders a script needs, and the first request for a folder becomes
an item in your queue. One grant covers a whole project. With several paths, "Remember
all" sets how long every one is remembered.

A mounted folder leaves out the files named like secrets: `.env` files, keys and
certificates, credentials files, Terraform state. A script reads each as empty. The
item lists what a mount leaves out, and the run's result tells the agent. An agent
that needs one of those files asks for the file by name. That request is its own
item, high risk, and takes your passkey.

The cover goes by file names. A token in an ordinary file is readable, and so is
anything in the repository's git history.

## Keep paths out of reach

Paths in `~/.config/maji-mcp/never-mount` are never mounted, whatever is granted. A
folder that holds a listed path is mounted with the listed path reading as empty. One
path a line, absolute or starting `~/`, with `#` comments:

```
# client work
~/src/github
~/Documents/contracts
```

An edit counts from the next run. [design/runner.md](design/runner.md) lists the paths
that are never mounted without any list.

## Try the web client without a stack

```sh
make demo
```

`make demo` runs a separate control plane on `http://localhost:8768`, with sample
requests, a passkey of its own named "maji-mcp on demo", and nothing ever applied.
`make demo RESET=1` empties the demo's queue and keeps its passkey. `RESET=all`
removes the passkey too.

## Back up and restore

```sh
make backup                    # into ~/.local/share/maji-mcp/backups
make restore FROM=<file>
```

A backup holds the queue's facts and diffs, which can include your data. The backup
directory is readable only by you and sits outside any synced folder. A restore
refuses a file that is damaged or belongs to another instance.

## Stop, and update

```sh
make down      # stops the containers; every volume is kept
make up        # rebuilds the images from the current source, and starts again
```

A restarted gateway ends every session, and a restarted broker forgets saved values.

## After the checkout moves

Claude Code's entry names `bin/maji-headers` inside the checkout, and Claude
Desktop's names `.venv/bin/maji-relay`. After the checkout moves or is renamed, add
both again:

```sh
make reconnect-claude-code              # Claude Code's entry names the new path
make connect-claude-desktop REPLACE=1   # with Claude Desktop closed
```

## Settings

Settings for `make`, given on the command line, such as `make up GATEWAY_PORT=9001`:

| Setting | Default | Meaning |
|---|---|---|
| `DOCKER_HOST` | the Docker CLI's current context | the Docker daemon every command uses |
| `PRINCIPAL` | `claude-code` | the principal a token is made for, and an app connects as |
| `SECRETS` | `~/.config/maji-mcp/secrets` | the secrets directory |
| `GATEWAY_PORT` | `8765` | the gateway's port on `127.0.0.1` |
| `CONTROL_PORT` | `8767` | the control plane's port on `localhost` |
| `BRIDGE_PORT` | `8766` | the bridge's port on `127.0.0.1` |
| `BRIDGE_ID` | `emacs-org` | the bridge's name: its token file, its pid file and its log are named for it, so the name is letters, digits, hyphens and underscores |
| `BRIDGE_SERVER` | emacs-org-mcp, from `EMACS_ORG_MCP` | the command of the stdio server the bridge serves |
| `STATE` | `~/.local/state/maji-mcp` | where the bridge's pid file and log are kept |
| `SCOPE` | `user` | the Claude Code scope `make connect-claude-code` adds `maji` to |
| `EMACS_ORG_MCP` | `~/src/emacs-org-mcp` | the checkout of [emacs-org-mcp](https://github.com/scanner/emacs-org-mcp) that `make bridge` serves |
| `ORG_DIR` | emacs-org-mcp's own | the org directory `make bridge` serves |
| `BACKUPS` | `~/.local/share/maji-mcp/backups` | where `make backup` writes |
| `DEMO_DIR`, `DEMO_PORT` | `~/.cache/maji-mcp/demo`, `8768` | the demo's data and port |
| `DESKTOP_CONFIG` | Claude Desktop's settings file | the file `make connect-claude-desktop` changes |
| `REPLACE` | unset | `REPLACE=1` lets `make connect-claude-desktop` replace a different `maji` entry |
| `TAG` | `latest` | the tag the images are built and run under |

Settings `compose.yaml` reads from the environment, for a stack started without
`make`:

| Variable | Default | Meaning |
|---|---|---|
| `MAJI_PREFIX` | `maji` | names the project and its volumes, so two stacks share nothing |
| `MAJI_GATEWAY_PORT`, `MAJI_CONTROL_PORT` | `8765`, `8767` | the published ports |
| `MAJI_SECRETS` | `~/.config/maji-mcp/secrets` | the secrets directory |
| `MAJI_SECRETS_DIR` | `~/.config/maji-mcp/secrets` | the same directory as an absolute path, which the runner never mounts |
| `MAJI_POLICY`, `MAJI_SERVERS` | `./config/policy.json`, `./config/servers.json` | the broker's policy and servers files |
| `MAJI_SERVICE_IMAGE`, `MAJI_RUNTIME_IMAGE` | `maji-service:latest`, `maji-python-runtime:latest` | the images to run |
| `MAJI_MOUNT_ROOT` | `$HOME` | the directory a mounted path must be inside |
| `MAJI_NEVER_MOUNT` | `~/.config/maji-mcp/never-mount` | your never-mount list |
| `MAJI_DOCKER_GID` | `102`, Rancher Desktop's | the group that owns the Docker socket, as a container sees the socket. `make up` reads the group from the socket, so set this only for a stack started without `make`, or to override what `make up` reads. |
| `MAJI_PUBLISHER` | `me` | the namespace skills are saved under |
| `SENTRY_URL` | `https://sentry.io` | the Sentry host |
| `GITHUB_API_URL` | `https://api.github.com` | GitHub's API; a GitHub Enterprise Server's is `https://<host>/api/v3` |

## When something goes wrong

```sh
make status                        # the containers, and the control plane's health
make logs                          # the last 200 lines from every service
make logs SERVICE=gateway FOLLOW=1 # one service, followed
make restart SERVICE=broker        # restart one service
make shell SERVICE=broker          # a shell in one service's container
make bridge-logs                   # what the bridge wrote
make library-use DAYS=7            # how scripts used each runtime library
```

| What you see | What to do |
|---|---|
| `maji` does not connect in Claude Code | `make status`. If the stack is up, `make reconnect-claude-code`: it makes a new token, restarts the gateway, and adds `maji` again in place of any it finds. An app still holding the old token stops working with it. |
| Claude Desktop shows `maji` as failed | The stack was not up when Claude Desktop opened, or the token was refused. Start the stack, then quit and reopen Claude Desktop. `~/Library/Logs/Claude/mcp-server-maji.log` has the relay's message. |
| A script cannot call `org.*` | `make status` says whether the bridge is running. `make bridge` starts it and restarts the broker. If it is running, `make restart SERVICE=broker` connects the broker again, and `make bridge-logs` shows what the bridge wrote. |
| A script cannot call `sentry.*`, `jira.*` or `github.*` | The service isn't connected: the script's error names a Connect item in your queue. Paste a key into it. If the Connect decision's outcome says it failed, it says why: the service refused the key, the key can't read the Sentry organization or Jira site named, or the key didn't reach the broker; connect the service again from the Connections page. A Jira token expires after a year at most. |
| A GitHub repository is `not_found` | The key can't see it: the token doesn't include the repository, or the repository's organization hasn't approved the token. Make a token that includes it, and connect GitHub again. |
| A stub call is `permission_denied` | `config/policy.json` does not allow the principal that stub, or the stub writes. |
| A script reads a file as empty | A mount left the file out, because the file is named like a secret or is on your never-mount list. The run's result says which files. The agent can ask for the file by name. |
| A folder is refused as holding too much to look through | The runner could not check the folder for files named like secrets within 4 seconds or 50,000 entries. The agent asks for a folder inside it. |
| Every mount is refused | A line in your never-mount list cannot be used. `make logs SERVICE=runner` names the line. |
| A grant "for this session" stopped holding | The session ended. `make logs SERVICE=gateway` shows each session's start and end. |
| The setup link has expired | `make up` again gives a new link. |
| `run_python` says a path waits on an item | Decide the item in the queue, then tell the agent to run the request again. |
| An agent says a skill was demoted | You demoted that version. The agent can keep a changed version, which runs as the agent's own. |
