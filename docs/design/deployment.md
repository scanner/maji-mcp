# Deployment

maji-mcp runs as four containers under Docker Compose, on your own machine. This
document covers what runs where, the volumes and ports, what was measured, and who
owns the Docker daemon.

**Status:** this document describes what is built: one instance on one machine, under
your own Docker daemon. The options for a daemon under another user and for a remote
host are designed and not built. [../using.md](../using.md) has the commands and the
settings.

## What runs where

| Part | Where | Lifetime |
|---|---|---|
| Gateway | container | long-lived |
| Control plane, with the web client | container | long-lived |
| Broker | container | long-lived |
| Runner | container | long-lived; the only holder of the Docker API |
| Executor | container from the runtime image | one for each run |
| Bridge, with a server such as emacs-org-mcp | your machine, as you | from `make bridge` until `make bridge-stop`, or until you log out |
| Relay | your machine, as you | while Claude Desktop runs |
| Apps, your browser | your machine, as you | your own |

Containers give each component its isolation, and let one definition run on a laptop,
a desktop or a Linux server. Servers that act as you, on your files or your editor,
run on your machine under your user, and the broker reaches them through a bridge.

## The compose stack

`compose.yaml` defines one project, named `maji` unless `MAJI_PREFIX` says otherwise.
A second stack with another prefix, such as a test's, shares nothing with the first.

**Every service container** starts from the service image, as user `10002`, with a
read-only root filesystem, a tmpfs at `/tmp`, every capability dropped and
`no-new-privileges`.

| Service | Network | Mounts beyond its own volumes |
|---|---|---|
| gateway | publishes `127.0.0.1:8765` | the gateway token hashes, as a secret |
| control | publishes `127.0.0.1:8767` | the instance id and the client token hashes, as secrets |
| broker | outbound, and your machine through `host.docker.internal` | the policy and servers files; the bridge token, as a secret. The keys you connect services with are on its own volume, `maji-credentials`. |
| runner | none | the Docker socket; your home directory, read-only, at the same path |

**The runner reaches the Docker socket through the socket's group,** never as root.
The group is the one a container sees on the socket, and differs from one Docker to
another: Rancher Desktop's socket inside the VM is `root:102`, mode 660. `make up`
reads the group from the socket, with a container of the service image, and passes
the group to the stack as `MAJI_DOCKER_GID`.

### Volumes

Every volume is a named volume. The containers that mount a volume are the only
containers that can reach what the volume holds.

| Volume | Holds | Mounted by |
|---|---|---|
| `maji-broker-call` | the broker's call socket | broker; every executor, read-only |
| `maji-broker-control` | the broker's control socket | broker, runner, gateway |
| `maji-runner` | the runner's socket | runner, gateway |
| `maji-run-log` | the run log | broker |
| `maji-library` | the skill library | broker |
| `maji-credentials` | the keys you connect services with, each readable only by the broker's user ([connections.md](connections.md)) | broker |
| `maji-control-data` | the control plane's database | control |
| `maji-control-broker` | the control plane's socket for the broker | control, broker |
| `maji-control-gateway` | the control plane's socket for the gateway | control |

A socket on a named volume works between containers because the containers run on one
VM kernel. `make down` keeps every volume.

### Ports

Two ports are published, both on `127.0.0.1` only.

- **The gateway, 8765:** MCP over streamable HTTP, with one gateway token for each
  principal.
- **The control plane, 8767:** the REST API and the web client. The control plane is
  reached at `http://localhost:8767`, because a passkey cannot be bound to an IP
  address.

### Secrets

`make setup` creates each missing file in the secrets directory, `~/.config/maji-mcp/secrets`, and
never replaces a file. The directory is readable only by you, each file is created
with mode 600, and no token is ever printed.

The secrets directory is outside the checkout on purpose. The runner refuses to
mount any folder that holds the secrets directory, so secrets kept inside a project
would stop a script from being granted the project as one path. A hidden folder at
the top of your home directory is never mounted at all ([runner.md](runner.md)).

| File | Holds |
|---|---|
| `gateway_tokens.json` | the SHA-256 hash of each principal's gateway token |
| `<principal>.token` | the principal's gateway token, for the headers helper and the relay |
| `emacs-org.token` | the token the bridge asks for, read by the bridge and the broker |
| `instance` | the instance id, fixed once made because it goes into signed hashes |
| `control_clients.json`, `control-cli.token` | the hash of each client token, and a token for a command-line client |

Compose mounts each file into the one container that needs the file. No secret is in
an image or in the repository.

## The images

| Image | Built from | Holds |
|---|---|---|
| `maji-service` | `images/service/Dockerfile` | the Python package with its locked dependencies, and the built web client |
| `maji-python-runtime` | `images/python-runtime/Dockerfile` | Python, git, the `maji` module, and the runtime libraries a script can import, pinned |

- **Both base images are pinned by digest.**
- **The service image installs from `uv.lock` and `web/package-lock.json`,** so the
  image runs exactly what the tests ran. A Node stage builds the web client, and
  generates the client's types from `api/openapi.yaml` as it builds, so a client
  that has drifted from the contract fails the build.
- **The service image runs as user 10002, and an executor as user 10001.** The
  directories that hold sockets, the run log and the library belong to user 10002, so
  a named volume mounted over a directory starts out owned by user 10002.
- **`make images` builds both images as `:latest`,** which the stack runs.
  `make test-docker` builds both as `:test` and tests those, so a test run never
  replaces the images a running stack uses.

[runner.md](runner.md) covers what the runtime image holds and how a library is added.

## The Docker API belongs to the runner

Whoever can reach the Docker socket controls every container, and the VM the
containers run in. With the socket, code can:

- `docker exec` into the broker and read the broker's credentials
- start a container with network access, or a privileged container
- mount your home directory, which Docker on a Mac shares into the VM

The socket therefore goes only to the runner, which is trusted and never runs agent
code.

- **A run request names a runtime and a script, and nothing about the container.** The
  runner chooses the image from an allowlist, the mounts from the broker's grants,
  and the settings from a fixed list ([runner.md](runner.md)).
- **Filtering which Docker API endpoints a client may call is not enough.** A
  permitted "create container" call can still ask for a privileged container or a
  host mount. The runner checks every container the runner creates.
- **Docker's file sharing does not apply your file permissions to container users,**
  as measured with Rancher Desktop. The grant is therefore the only thing between an
  executor and your files.

## Who owns the Docker daemon

**The built deployment is option (a): the daemon runs under your own user.**
Agent-written code is always sandboxed. The rest of the protection is proportionate:
maji-mcp adds a better path to the services an agent already reaches directly, and
puts convenience first.

The trade-off: the daemon's socket is owned by you. Rancher Desktop's is
`~/.rd/docker.sock`, with mode 600, and Docker Desktop's is in `~/.docker`. A program
running as you, such as an agent with a shell, could `docker exec` into the broker
and read the broker's credentials.

| Option | Executor isolation | Broker credentials against your shell | Status |
|---|---|---|---|
| **(a) Daemon under your user,** as Rancher Desktop and Docker Desktop run | full | reachable through `docker exec` | built |
| **(b) A container VM started by a separate macOS user,** such as Lima or Colima, with a socket only that user can open | full | out of reach | untested |
| **(c) A Linux host with rootless Docker under a service user,** optionally with gVisor | full, with a stronger kernel boundary | out of reach | not built |

- **(c) is the remote option,** for when running locally is not practical, such as an
  agent on an iPad. The gateway is then reached over the network, so the gateway
  needs TLS or a private network in front of it, as well as the gateway token. The
  control plane needs a name and a certificate, which passkeys require. Servers that
  act as you run on the remote machine.
- **Removing the agent's shell is another option.** An agent whose app has no shell
  reaches your systems only through maji-mcp, and cannot use the Docker socket. The
  app's file tools can still read anything your user can, such as a token file, so
  the option also means limiting which directories the file tools reach.

## Reaching servers on your machine

- **A container cannot use a Unix socket served on the Mac.** The container runs on
  the VM's kernel. A server that acts as you on a Mac therefore serves HTTP on
  `127.0.0.1` with a token, and the broker reaches the server at
  `host.docker.internal`. The bridge does this for a stdio server
  ([broker.md](broker.md)).
- **Any container with network access can reach the Mac's loopback.** Executors
  therefore get no network.

## Measured

On an arm64 Mac, with Rancher Desktop and Docker Engine 29.1.3:

| Test | Result |
|---|---|
| An executor run of a short script, from create to remove | about 230 ms |
| `docker exec` into a running container | 48–52 ms |
| A Unix socket served on the Mac, bind-mounted into a container | connection refused |
| A Unix socket between two containers on a named volume, both with no network | works |
| A service on the Mac's `127.0.0.1`, from a container through `host.docker.internal` | works |
| A socket on a named volume, served by user 10002, reached by user 10001 through a read-only mount | works |
| A container running as root with every capability dropped, writing to a directory another user owns | refused |
| One emacs-org read from the broker, through the bridge to emacs-org-mcp on the Mac | about 0.7 s |
| One Sentry issues query from the broker, over a 14-day window | about 4 s |

The last refusal is why the runner runs as the service user with the socket's group
added, never as root.

## Planned

- **Build containers and toolchain images,** one pair for each runtime, with a build
  cache and a dependency mirror ([planned/languages.md](planned/languages.md)).
- **The skill library on a directory a file-sync tool can sync** within a persona
  ([planned/sharing.md](planned/sharing.md)).
- **A registration directory the broker watches,** so a server that starts or stops
  is seen without a restart
  ([planned/server-contract.md](planned/server-contract.md), B6).
- **Options (b) and (c),** each with a guide.
- **A secret store** such as Vault or 1Password in place of files, with short-lived,
  narrowly scoped tokens.
- **gVisor for executors** on a Linux host.
