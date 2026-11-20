# Security model

What maji-mcp protects, what each protection rests on, and what maji-mcp does not
protect.

**Status:** this document describes what is built. Each section links to the document
that holds the detail.

## What maji-mcp is protecting

maji-mcp lets agent-written code work with your services and your files. Three things
must hold while the code runs:

1. **The code never holds a credential.** The keys you connect services with, such as
   Sentry's, the bridge token and every later credential stay in the broker.
2. **The code reaches only what a check allowed.** A stub call passes the broker's
   policy. A file is readable only inside a path you granted.
3. **The code never decides.** A decision in the queue needs your passkey. No token
   an agent holds can make a decision.

Agent-written code is treated as hostile. An agent reads text that other people wrote,
such as error messages and ticket bodies. Text of that kind can carry instructions
aimed at the agent, so a script can do something the agent's user never asked for.

## The protections

| Protection | Rests on | Detail |
|---|---|---|
| A script has no network | The runner starts every executor with `--network none`. | [runner.md](runner.md) |
| A script has no credentials | An executor's only secret is the run token. The run token works for one run and only for stubs the run's principal may call. | [architecture.md](architecture.md) |
| A script cannot change the executor's image or your files | The executor's root filesystem and every mount are read-only. The workspace is a tmpfs that is removed with the executor. | [runner.md](runner.md) |
| A script cannot exhaust the machine | Memory, CPU, process-count, time and output limits on every executor. | [runner.md](runner.md) |
| A script cannot choose its sandbox | A run request names a runtime and a script. The runner builds the container settings and then checks the exact settings against an allowlist before the Docker API sees them. | [runner.md](runner.md) |
| A script cannot claim to be another principal | The broker finds the principal from the run token, and ignores any principal a call names. | [architecture.md](architecture.md) |
| A principal calls only the stubs you listed | The broker's policy file, checked on every call. A principal the policy does not name may call nothing. | [broker.md](broker.md) |
| A loop cannot flood a service | A rate limit per principal and namespace. | [broker.md](broker.md) |
| A server's tool is exposed only when you list it | The broker's servers file names each tool and whether the tool reads or writes. The broker refuses every write stub today. | [broker.md](broker.md) |
| A script reads only granted paths | The broker names a run's mounts from grants. The runner resolves each path twice and binds only the granted paths, read-only. | [runner.md](runner.md) |
| Some paths are never mounted | Built into the runner: your home directory itself, hidden folders at the top of your home directory, `~/Library`, and maji-mcp's secrets directory. Your never-mount list adds more. | [runner.md](runner.md) |
| A mounted folder leaves out what a script is not to read | The runner covers each file named like a secret, and each entry of your never-mount list inside the folder: a script reads each as empty. A cover is the empty device or an empty file system, read-only, and a script cannot take a cover off. | [runner.md](runner.md) |
| A file named like a secret is read only on its own grant | The broker asks the control plane for a grant on exactly that path, and the item is high risk. | [runner.md](runner.md) |
| A token never decides | The control plane verifies a passkey assertion, or a tab key's signature, over a hash of the exact choices. | [control-plane.md](control-plane.md) |
| You sign what you saw | A choice carries the content hash of the item as shown. A changed item is not decided. | [control-plane.md](control-plane.md) |
| An agent's words do not pass for facts | The broker computes an item's facts and risk. A client shows the requester's claim after the facts, labelled as the requester's words, as plain text. | [client-rules.md](client-rules.md) |
| History cannot be rewritten through the service | Database triggers refuse any update or delete of decisions, outcomes, grants, revocations and events. | [control-plane.md](control-plane.md) |
| Only the broker writes the skill library | The library is on a volume only the broker mounts. | [skills.md](skills.md) |
| Another site cannot act through your browser | The control plane checks the `Host` and `Origin` headers, sets the sign-in cookie `SameSite=Strict`, and sends a content security policy that allows only the control plane's own files. | [control-plane.md](control-plane.md) |
| Secrets stay out of the images, the repository and every mount | The secrets directory is outside the checkout, in a hidden folder of your home directory that the runner never mounts. Each secret file is created with mode 600. No token is ever printed. | [deployment.md](deployment.md) |
| The runtime image holds only reviewed libraries | The image installs the runtime libraries from PyPI only, as wheels, each pinned by hash, each release at least 14 days old. | [runner.md](runner.md) |

## What the runner is trusted with

Whoever reaches the Docker socket controls every container, and can mount any file
the Docker daemon can see. The Docker socket therefore goes to the runner alone. The
runner never runs agent code. The runner also sees your home directory read-only, to
resolve paths the same way the Docker daemon will. The runner already holds the
Docker socket, so the read-only view gives the runner nothing more.

## What maji-mcp does not protect

- **Your user account from itself.** In the default deployment your user owns the
  Docker daemon. A program running as you can use `docker exec` to read the broker's
  credentials or the control plane's database. An agent with a shell is such a
  program. maji-mcp sandboxes the scripts agents send to `run_python`. maji-mcp does
  not sandbox the agent's own app. [deployment.md](deployment.md) lists three ways to
  close the gap: a Docker daemon under another user, a remote host, or an agent with
  no shell.
- **The secrets directory from your user.** A program running as you can read
  `~/.config/maji-mcp/secrets`, the gateway token included.
- **The setup link before the first passkey.** Until a passkey is registered, a
  program running as you could use the one-time setup link first. Register the first
  passkey as soon as the stack is up, and check the Passkeys page afterwards.
- **A passkey prompt from a misleading page.** An authenticator shows the site that
  asks, never the content being approved. The signature is only as good as the
  client's display of the item. [client-rules.md](client-rules.md) states what a
  client must show.
- **Secrets under an ordinary name, or in git history.** The runner covers the files
  in a granted folder that are named the way secrets are usually kept. The cover goes
  by file names: a token pasted into an ordinary file is readable, and so is a secret
  that was once committed, through the repository's history.
- **Data a script prints.** What a script prints goes to the agent, and from the
  agent to the agent's provider. The broker's policy decides what a script can fetch.
  Nothing filters what a script prints.
- **A mounted file that a library runs as code.** Some calls build code from data:
  `yaml.unsafe_load`, or `yaml.load` with `yaml.Loader`, rendering a Jinja template,
  and unpickling, with `pickle` or `pandas.read_pickle`. A script that uses one on a
  file from a mounted repository runs whatever the file says, with the run's stubs.
  The libraries' notes steer agents to the safe calls, `yaml.safe_load` and parsing a
  template without rendering it, but nothing stops a script making the unsafe ones.
  The sandbox still holds: such code has no network and reads only granted paths.
- **A server that acts as you.** The bridge and the server behind the bridge run as
  you. A program running as you could stop either one and take its place.
- **Signatures on skills.** A promotion is a signed decision in the control plane's
  database. The broker does not yet verify a signature before loading a skill. The
  broker's volume is the boundary until signature checks are built
  ([planned/sharing.md](planned/sharing.md)).

## A high-risk label is a finding

The broker labels an item high risk for one of six reasons
([control-plane.md](control-plane.md)). The label says what the broker found or
suspects. An item with no label is an item where the broker found none of the six
reasons. No client calls an item safe, low risk or checked.
