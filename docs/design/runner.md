# The runner and the executor

The runner starts one executor for each run. The executor is the sandbox a script runs
in. This document covers the sandbox, the runtime image, and how a script reads your
files through mounts.

**Status:** this document describes what is built. Code: `src/maji_mcp/runner/`,
`src/maji_mcp/executor/`, `images/python-runtime/`.

## What the runner does for one run

The runner serves one Unix socket, on a volume only the gateway mounts. A `run`
request names the principal, the runtime, the script and any paths to mount. The
runner then:

1. resolves each path, and refuses a path that is never mounted
2. registers the run with the broker, which answers with a run id and a run token, or
   with the item that asks you to grant the paths
3. resolves each path a second time, works out what each mount leaves out, builds the
   container settings, checks the settings, and starts an executor
4. sends the script on the executor's standard input, waits up to the time limit,
   reads the output and removes the executor, whatever happened
5. finishes the run with the broker, which revokes the run token, logs the outcome
   and reports the stubs and skills the run called

The runner is the only component that holds the Docker API. The runner has no network
and never runs agent code.

## One executor per run

Each run gets a fresh container. A fresh container gives a script a clean filesystem
with nothing left by an earlier run. A short script takes about 230 ms from creating
the executor to removing the executor, measured on an arm64 Mac with Rancher Desktop.
230 ms is small beside one turn of an agent.

A script goes in on standard input. No script is written to a shared volume, so no
run can read another run's source.

No executor outlives its runner for long. The runner holds a script to its deadline by
waiting on the script's executor, so an executor left running by a runner that has
gone would run with no deadline. A runner told to stop therefore takes no more
connections, kills the executor of every run in flight, and removes each container. The runner finishes
each of those runs with the broker, which ends the run's token, and answers each as
`unavailable`: the agent is told that the runner is stopping, and to run the script
again. The runner then exits. The runner waits at most 5 seconds for those runs to be
finished and answered.

A runner that crashes, or is killed outright, has no chance to do any of this, and
leaves the executor of a script in flight running. Compose starts a runner that
crashed again, and a runner that starts removes every executor an earlier runner
left, before the runner takes a run of its own. The runner then tells the broker that
each of those runs is over, which ends the run's token. The runner's log says how
many executors the runner removed.

An executor belongs to a runner when the executor mounts the socket volume of the
runner's own broker. Two stacks on one Docker daemon, such as yours and a test's,
have different socket volumes, so one stack's runner never removes the other stack's
executors.

## The sandbox

The runner applies the same settings to every executor, whatever the request says
(`src/maji_mcp/runner/sandbox.py`).

| Setting | Value |
|---|---|
| Network | none |
| Root filesystem | read-only |
| Workspace | a tmpfs at `/work`, and a tmpfs at `/tmp`; both removed with the executor |
| User | `10001:10001`, never root |
| Capabilities | all dropped, with `no-new-privileges`; never privileged |
| Volumes | one: the broker's call socket, read-only |
| Host paths | only the paths the broker granted the run, each read-only at `/mnt/<name>`, with covers over what a mount leaves out |
| Environment | `MAJI_RUN_TOKEN` and `MAJI_BROKER_SOCKET`, and nothing from the runner's own environment |

| Limit | Default | Most a request may ask |
|---|---|---|
| Memory | 512 MB, with no swap beyond it | 4 GB |
| CPU | 1 | 4 |
| Processes | 128 | 512 |
| Time | 60 s | 600 s |
| Workspace | 64 MB | 1 GB |
| Output | 64 KB | 1 MB |

`run_python` lets an agent change only the time limit.

**The settings are built, then checked.** `container_config` builds the arguments for
the Docker API. `validate` then checks the exact arguments: every key must be on an
allowlist, the image must be a runtime image the runner was configured with, and each
bind mount must be a path the broker granted, read-only. `validate` refuses keys such
as `privileged`, `devices`, `cap_add` and `pid_mode` outright. A mistake in the
builder is therefore refused by the check and never reaches the Docker daemon.

**A bind mount excludes anything mounted beneath it.** A read-only bind is read-only
only at its top, so the runner binds each path non-recursively.

## The runtime image

`images/python-runtime/Dockerfile` builds `maji-python-runtime`.

- **Base:** `python:3.14-slim`, pinned by digest.
- **User:** `10001`, the same user the runner names.
- **The `maji` module:** `src/maji_mcp/executor/maji.py` is copied in as
  `/opt/maji/maji.py`. The module uses the standard library only.
- **The entry point:** `src/maji_mcp/executor/run.py` reads the script from standard
  input, makes `maji`, `skills` and each namespace the run may call a global, and runs
  the script as `__main__`. Python starts with `-I`, so the script's directory and
  the user site directory are left out of the import path.
- **Git,** from Debian's archive, for scripts that read mounted repositories.
- **The runtime libraries,** what a script can import beyond the standard library,
  listed in `images/python-runtime/requirements.in`:

  | Import | For |
  |---|---|
  | `yaml` (PyYAML) | YAML files |
  | `jinja2` (Jinja2) | parsing Jinja templates |
  | `markdown_it` (markdown-it-py) | parsing Markdown |
  | `pygments` (Pygments) | tokens for about 500 languages |
  | `tree_sitter` (tree-sitter, with grammars for JavaScript, TypeScript and TSX, Go, Rust, Ruby and Perl 5) | syntax trees for languages other than Python |
  | `unidiff` | unified diffs |
  | `pathspec` | `.gitignore`-style patterns, such as CODEOWNERS |
  | `rapidfuzz` (RapidFuzz) | fuzzy matching |
  | `duckdb` | SQL over CSV, JSON-lines and Parquet files, and DataFrames |
  | `pandas`, with numpy | DataFrames |
  | `git` (GitPython) | a repository's objects |

  With them the image is about 510 MB; it was 259 MB with GitPython alone. Agents
  learn of them as [agent-guidance.md](agent-guidance.md) describes.

A script has no network, so a library that downloads something on first use fails.
duckdb's extensions for SQLite and remote files are such downloads: a script reads a
SQLite database with `maji.sqlite`. duckdb keeps within 80% of the run's memory and
spills to `/work`.

### Adding a runtime library

1. Add the library to `images/python-runtime/requirements.in`.
2. Run `make runtime-lock`. The lock resolves from PyPI alone, whatever index uv or
   pip is configured with on your machine. The lock takes only releases at least 14
   days old, so a release withdrawn soon after publication never reaches the image.
   It takes only wheels, as the image installs only wheels, and records a hash of
   every wheel for every platform.
3. Add the library's entry to `src/maji_mcp/common/runtime_libraries.py`: its import
   name, each distribution with its pinned version, a summary, and notes on where to
   start and what to watch for. A test checks the entries against `requirements.in`
   and the lock, so a lock that moves a version fails it too; that is when to read
   the library's changes and correct its notes.
4. Name the library in `run_python`'s description in
   `src/maji_mcp/gateway/server.py`. A test checks the description against the list.
5. Try the library in an executor under the default limits, with no network, before
   writing its notes: a test in `tests/docker/test_libraries.py` is the pattern.

The image installs nothing without a matching hash, and builds nothing from source.

## What a script has

| In a script | What it is |
|---|---|
| `sentry`, `org`, … | Each namespace the principal may call, as a global. `sentry.issues(project="api")` is one stub call. |
| `skills` | The skills the principal may call: `skills.culprits(project="api")` ([skills.md](skills.md)). |
| `maji.call(name, **args)` | A stub call by name. |
| `maji.parallel(fn, items)` | Runs `fn` over `items` on up to 16 threads and returns the results in order. Each thread has its own connection to the broker. |
| `maji.save(name, value)`, `maji.load(name)`, `maji.saved()` | Saved values for the session's later scripts ([broker.md](broker.md)). |
| `maji.mounts()` | The paths mounted for the run. |
| `maji.git(name, *args)` | Runs git on a mounted repository. |
| `maji.git_log(name, since=..., paths=...)` | Each commit with the files the commit changed, from one git call. |
| `maji.blame(name, path, line)` | The commit behind one line. |
| `maji.sqlite(name, path)` | Opens a mounted SQLite database read-only. |
| `maji.StubError` | Raised by a failed stub call, with `.code`. |

The mount helpers run in the executor and never call the broker.

## Mounts: reading your files

A script reads a file only when the file's path is mounted into the script's executor.
An agent names up to 10 paths in `run_python`'s `paths`. A path can be a git
repository, a folder inside a repository, a folder of profiles or log captures, or one
file.

### The rules

- **A path must be inside the mount root,** your home directory. Docker on a Mac
  shares your home directory with its VM, and Rancher Desktop shares nothing else, so
  a path elsewhere is not mounted.
- **The runner resolves each path, because only the runner can.** The Docker daemon
  resolves a bind source itself, so a path through a symlink binds wherever the
  symlink leads. The runner sees your home directory read-only at the same path, and
  resolves each path the way the daemon will. The item you decide shows the resolved
  path, and the grant covers the resolved path.
- **The runner resolves each path twice:** before the broker sees the path, and again
  just before the executor is created. The runner binds a path only when the second
  result is still the granted path.
- **The mounts come from the broker's grants, never from the request.** The runner
  binds exactly the paths the broker's answer names.
- **Each path is mounted read-only at `/mnt/<name>`,** where `<name>` is the last part
  of the path. Nothing beyond the path is mounted.
- **A grant on a folder covers everything in the folder,** including a folder inside
  it asked for later, apart from what a mount leaves out (below). The control plane
  compares whole path segments, so a grant on `~/src/api` does not cover
  `~/src/api-old`.
- **A refusal covers exactly the path refused.** A folder refused as too broad, such
  as `~/src/github`, does not stop a request for a folder inside it.

[control-plane.md](control-plane.md) covers the item, the grant durations, and what
happens when an agent runs the request again.

### Paths never mounted

The runner refuses these paths whatever is granted. The list is built into the code
(`src/maji_mcp/runner/mounts.py`), and removing an entry is a change to maji-mcp, never
a setting.

- the mount root itself: your whole home directory
- anything in a hidden folder at the top of your home directory, such as `~/.ssh`,
  `~/.config`, or `~/.rd` and `~/.docker`, which hold the Docker sockets of Rancher
  Desktop and Docker Desktop
- `~/Library`
- maji-mcp's secrets directory, and any folder that holds the secrets directory

The runner does not start without the location of the secrets directory
(`--secrets-dir`), so the one entry that comes from configuration cannot be left out.
The secrets directory is `~/.config/maji-mcp/secrets` by default: outside every
project, so the entry stops no project from being mounted.

### What a mount leaves out

A project is one grant. An agent that works in a repository already reads everything
in the repository through its own app, so asking again for each folder would put
hoops in front of access the agent already has. One request for a repository, treated
the same way whichever repository it is, is the whole of the asking.

A project can still hold files a script has no need of and should not read: a `.env`
with credentials for testing, a private key, a folder you keep to yourself. The runner
mounts the folder and **covers** each of those inside the executor.

- **A covered file reads as an empty file, and a covered folder lists as an empty
  folder.** The script sees that the name is there, and nothing in it.
- **Two kinds of path are covered:** the files named the way secrets are usually
  kept, and the entries of your never-mount list that are inside the folder.
- **The item says what a mount leaves out,** by name, before you grant the path. A
  grant on a folder was therefore never a grant to read what the folder's mount
  leaves out.
- **The run's result says what was left out,** so an agent does not take an empty
  read for an empty file. Entries of your never-mount list are counted and not named.
- **The covers are worked out before every run,** because files come and go
  (`covers_in`). A `.env` created after the grant is covered from the next run.

**How a cover is made** (`Cover` in `src/maji_mcp/runner/sandbox.py`). A file is
covered by the kernel's empty device, `/dev/null`, bound read-only over the file's
path. A folder is covered by an empty file system of its own, mounted read-only. A
cover holds nothing of yours, so a cover can only take away what a script reads. The
check on the container's settings accepts a cover only inside a granted mount, only
read-only, only from the empty device or with the fixed settings of the empty file
system, and never inside another cover. A script runs with every capability dropped,
so a script cannot take a cover off.

**Which files are named like secrets** (`src/maji_mcp/runner/secrets.py`):

- `.env` files, and not `.env.example`
- keys and certificates, such as `id_rsa`, `*.pem` and `*.key`
- `credentials` and `secret` files that hold data, such as `client_secret.json`
- `.netrc`, `.npmrc`, `.pypirc`, kubeconfigs and Terraform state
- a repository's `.git/config`, when a remote URL in it carries a password or token.
  Git reads the repository without its settings, and history still reads.

**Reading a file that is left out.** An agent names the file itself in `paths`, such
as `~/src/api/.env`. That request needs a grant on exactly that path: a grant on a
folder that holds the file does not cover it. The item is high risk, with the reason
*secrets in reach*, and takes your passkey. A run that names both the folder and the
file mounts the file, and leaves it readable inside the folder's mount too.

**What the look costs, and when it refuses.** The runner sees your files through
Docker's file share, which lists about 10,000 entries a second with Rancher Desktop. A
repository of a few thousand files adds a fraction of a second to each run. The look
skips dependency and cache folders, such as `node_modules` and `.venv`, and does not
go into `.git`. The look has 4 seconds for all the paths of a run, and 50,000 entries
for each path.

- **A folder the look cannot finish is not mounted.** Some of what a script is not to
  read could go uncovered, so the run is refused, and the agent is told to ask for a
  folder inside it.
- **A folder with more than 200 paths to cover is not mounted,** for the same reason.
- **A file removed between the look and the start of the executor** fails the start.
  Nothing runs, and the agent is told to run the script again.

**What a cover does not do.** The item's facts and this list are where the limits are
stated:

- **The cover goes by names.** A token pasted into an ordinary file is not covered.
- **Git history is not covered.** A secret that was once committed is still in the
  repository's `.git` folder, which a script can read with git.
- **A copy or a hard link under another name is not covered.**
- **A link is never covered.** Inside an executor a link leads wherever its target is
  there: to a covered file, which reads as empty, or to nothing.
- **A file that appears while a run is going is not covered in that run.**

### Your never-mount list

Your own list adds to the built-in list. The file is
`~/.config/maji-mcp/never-mount`, or the file `MAJI_NEVER_MOUNT` names. One path a
line, absolute or starting `~/`, with `#` comments:

```
# client work
~/src/github
~/Documents/contracts
```

- **The list only narrows.** A listed path is refused even when a grant already
  covers the path, and so is anything inside a listed path.
- **A folder that holds a listed path is mounted, with the listed path covered.** A
  listed path that does not exist yet has nothing to cover, and is covered from the
  run after it appears.
- **The runner reads the list before each run,** so an edit counts from the next run.
- **A list the runner cannot use refuses every mount** until the list is fixed.
  `make logs SERVICE=runner` names the line.
- **A refusal never tells the agent what the list holds.** The agent is told to ask
  you where else to look.

### Git on a read-only mount

A mounted repository belongs to you, and the executor runs as user 10001. A mounted
repository's own config can name commands for git to run. The runtime image therefore
sets, through `GIT_CONFIG_*` environment variables that outrank a repository's config:

- `safe.directory=*`, so git accepts a repository owned by another user
- `core.fsmonitor=false` and `core.hooksPath=/dev/null`, so git runs no command a
  repository names
- `core.pager=cat`, and `GIT_OPTIONAL_LOCKS=0`, so git writes nothing

The settings hold for the `maji` helpers, for GitPython, and for git run straight from
a script. External diff and textconv have no such setting, so the helpers pass
`--no-ext-diff` and `--no-textconv`.

Two limits follow from how git stores history:

- **A folder inside a repository has no history.** Git needs the repository's top,
  where `.git` is.
- **A worktree has no history alone.** A worktree's `.git` points into the main
  repository, so the main repository must be mounted with the worktree.

### SQLite through a mount

- **A finished capture is read whole in the mode that fits how its writer stopped.**
  A capture closed cleanly has no `-wal` file, and `maji.sqlite` opens it as
  immutable. A capture whose writer was killed keeps its newest rows in a `-wal` file,
  and `maji.sqlite` opens it with `mode=ro`, which reads the rows.
- **A database with a `-wal` file is mounted by its folder.** The runner refuses the
  file alone, because the file alone would leave out the newest rows.
- **A live database is not read reliably through a mount.** SQLite's WAL needs every
  reader and writer on one machine. A test with a database written on the Mac and
  read from a container showed a held connection stuck at 6,800 rows while the
  database passed 17,000.
- **A database still being written is read through snapshots its writer publishes.**
  The writer copies the database whole into a folder of snapshots, under a new name
  each time, and moves a link in the same folder, such as `worker.db`, to the newest
  copy. Mount the folder, never the one holding the live database, and open the
  link: `maji.sqlite("snapshots", "worker.db")`.
  - The link must be relative to the folder, so it leads to a snapshot inside the
    executor's mount. An absolute link names a path on your machine, which the
    executor doesn't have, and `maji.sqlite` answers `not_found`.
  - A snapshot never changes, so `maji.sqlite` opens it as immutable. A connection
    keeps the snapshot it opened; open the link again for newer rows. Through the
    mount, the link can lead to the previous snapshot for a short while after it
    moves.
  - Each snapshot has a name of its own because an immutable database must never
    change under its reader. Rewriting one file in place would change it.
  - Query a snapshot in place. A copy of a large capture doesn't fit in the
    sandbox's `/tmp`.

## Where code in a mount pays

The first measurements found `run_python` level in cost with an agent running Python
in its own shell ([../research/measurements.md](../research/measurements.md)). A
script over mounts therefore pays when the script needs something the agent's own
shell lacks:

- a service's credentials and the files, in one script
- several repositories beyond the one the agent is working in
- an agent with no shell at all

## Planned

- **Writes to a repository.** A real repository is never mounted writable: code that
  can write `.git/hooks` or `.git/config` runs as you the next time you use git
  there. A run's writes will come back as a git bundle that the runner fetches from,
  and land as a branch or a worktree after your decision
  ([planned/writes.md](planned/writes.md)).
- **A second source of mounts:** the broker's cache volume for logs downloaded from
  cloud storage ([planned/connections.md](planned/connections.md)).
- **A live capture queried on your machine,** through a bridge, read-only, because a
  mount cannot read a live database reliably.
- **Your never-mount list kept by the control plane** and edited in the web client,
  where adding an entry needs nothing and removing an entry takes the passkey.
- **A remembered look,** so a large folder is not listed again before every run. The
  look would be kept from one complete pass, and only what changed checked again.
- **A covered file that reads as a regular empty file.** A covered file is the empty
  device today, so a script that asks whether the path is a regular file is told no.
- **Folders named like secrets,** such as a `secrets/` folder of files with ordinary
  names. The look names files only, because a folder named `credentials` is often
  code.
- **Files git ignores, named on the item.** Not built, because listing ignored files
  means running git in the runner.
- **Build containers and more runtimes** ([planned/languages.md](planned/languages.md)).
- **A warm container per runtime,** with a process per run, if 230 ms per run becomes
  a cost. Runs would then share a filesystem, so this waits for a need.
- **More libraries, each added once a task shows the library pays:** Beautiful Soup
  for saved HTML, numerical packages, memray and a pprof reader for profiles.
