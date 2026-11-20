# Developing maji-mcp

How the repository is laid out, how to build and test a change, and how to debug a
running stack. [design/architecture.md](design/architecture.md) explains the
components the layout follows.

## Layout

```
README.md                  what maji-mcp is, and how to start
AGENTS.md, CLAUDE.md       the rules a coding agent follows in this repository
CONTRIBUTING.md            how to propose a change
SECURITY.md                how to report a vulnerability
CHANGELOG.md               what changed in each release
Makefile                   every command, in three groups: running, developing, debugging
compose.yaml               the stack: four services, their volumes, ports and secrets
pyproject.toml, uv.lock    the Python package and its locked dependencies
api/openapi.yaml           the control plane's REST contract
bin/maji-headers           the headers helper Claude Code runs on each connection
config/                    the broker's policy and servers files
docs/                      the documents (docs/README.md is the index)
images/
  service/                 the image for the gateway, runner, broker and control plane
  python-runtime/          the image scripts run in, with its pinned libraries
src/maji_mcp/              the Python package
tests/                     the Python tests
web/                       the web client: Vue 3, TypeScript, Vite
LICENSE                    BSD 3-Clause
```

### The Python package

The package is grouped by where code runs, which is also where the trust boundaries
are.

| Package | Runs | Holds |
|---|---|---|
| `gateway/` | container | the MCP server apps connect to |
| `runner/` | container | the sandbox settings, mounts, the look for secrets, the Docker client |
| `broker/` | container | stubs, the policy, the run log, saved values, services, the skill library |
| `control/` | container | the queue's database, REST API, passkeys, internal sockets, the demo |
| `executor/` | inside the sandbox | `maji.py`, copied into the runtime image as the module `maji`, and `run.py`, the image's entry point. Standard library only. |
| `host/` | your machine, as you | `bridge.py`, `relay.py` and `setup.py` |
| `common/` | everywhere | the JSON-RPC protocol over Unix sockets, session names, stopping on a signal, and the runtime libraries' notes |

Each of the four container components has a `server.py` with a `main`.
`pyproject.toml` gives every program a command: `maji-gateway`, `maji-runner`,
`maji-broker`, `maji-control`, `maji-bridge`, `maji-relay` and `maji-setup`.

Every module opens with a docstring that says what the module does and why. The
module docstrings are the detailed design. The documents in `docs/design/` give the
whole picture and point into the code.

### The tests

The Python tests mirror the package. The tests of
`src/maji_mcp/<package>/<module>.py` are in `tests/<package>/test_<module>.py`, so
the tests of `runner/mounts.py` are in `tests/runner/test_mounts.py`. A module with
many tests has a second file named for the topic, `test_<module>_<topic>.py`, such
as `tests/gateway/test_server_skills.py`. Three more folders hold the suites that
need more than the Python environment.

| Where | Needs | Run with |
|---|---|---|
| `tests/common/`, `tests/gateway/`, `tests/runner/`, `tests/broker/`, `tests/control/`, `tests/executor/`, `tests/host/` | nothing beyond the Python environment: each test starts real components on Unix sockets in a temporary directory | `make test` |
| `tests/docker/` | a Docker daemon; the tests start executors and whole stacks of their own, with their own volumes, ports and secrets | `make test-docker` |
| `tests/browser/` | Chrome; the test drives the built web client with a virtual passkey | `make test-browser` |
| `tests/local/` | a checkout of [emacs-org-mcp](https://github.com/scanner/emacs-org-mcp). The folder also holds `org_skills.py`, the code the first write skills start from, with its tests. | `make test-local` |
| `web/tests/` | Node; unit tests of the web client, with axe-core | `make web-check` |

`pytest` markers keep the suites apart: a plain `pytest` runs only the first row.

A fixture lives at the lowest level that covers every test that uses it.

| Where | Holds |
|---|---|
| a test module | the fixtures only that module uses |
| `tests/<package>/conftest.py` | the fixtures several modules of one package use |
| `tests/<package>/factories.py` | the package's factories |
| `tests/conftest.py` | the fixtures several packages use: a clock moved by hand, session keys, a broker on real sockets, a runner and a gateway in front of that broker, and a control plane and a skill library beside it |
| `tests/support/` | what a test imports rather than asks for: stand-ins for Docker and Sentry, a broker in a container, the bridge, builders of repositories and log captures, the software authenticator and the checker of responses against `api/openapi.yaml`, and paths into the repository |

The tests keep these rules:

- **Every test has a docstring in Gherkin form**: GIVEN, WHEN, THEN. The docstring
  describes what the code under test guarantees, never how the test is built.
- **Cases of one shape are one test.** `pytest.mark.parametrize` lists the cases, and
  each case has an id that says what the case is.
- **A value whose content doesn't matter comes from the `faker` fixture.** Faker is
  seeded for each test, so a failing test fails the same way when it is run again. A
  literal stays where the value is the point of the test: `.env`, an error code, a
  name that has to sort before another.
- **A structure built in several tests with small differences has a factory.** The
  factories are factory_boy classes, called through `build`, with what a test wants
  different passed as arguments: `ChangesetFactory.build(high_risk=True)`. The root
  `conftest.py` seeds their made-up values before each test, as Faker's are.
- **Independent facts about one result are checked independently.** Each fact goes
  in its own `with check:` block, from pytest-check, so one run reports every fact
  that is wrong. Steps that depend on each other use a plain `assert`, which stops
  the test.
- **Every fixture has a docstring** that says what the fixture gives a test. A
  fixture shared by a module or a session also says why sharing is safe.
- **A long test is written in sections.** Each section opens with a comment that
  says what the section shows.

## A first build

```sh
make install     # uv sync, and npm ci for the web client
make check       # ruff, the formatter's check, mypy
make test        # the tests that need no Docker, checkout or browser
```

`uv run pre-commit install` adds the pre-commit hooks: whitespace, ruff and mypy.

## The checks a change should pass

```sh
make test-all    # check, test, web-check, test-browser and test-docker
```

`make test-all` leaves out `make test-local`, which needs emacs-org-mcp.

- **`make format`** fixes what ruff can fix, and formats.
- **`make web-check`** generates the web client's types from `api/openapi.yaml`,
  checks types and runs the client's tests. A client that has drifted from the
  contract fails here.
- **`make test-docker`** builds both images under the tag `test` and tests those.
  The stack on your machine runs `latest`, and the runner starts every executor from
  the runtime image's tag. A test run therefore never changes what a running stack
  runs. `make clean` removes the `test` images.
- **Tests never touch a running stack.** Each Docker test uses its own project
  prefix, volumes, ports and secrets.

## Working on a part

**A stub or a service.** Stubs are defined in `src/maji_mcp/broker/` with a name,
parameters, documentation and a handler (`catalog.py`). `sentry.py` is the model for a
REST service, and `mcp_servers.py` for an MCP server. The policy must allow a
principal the new namespace.

**A library for scripts.** Add the library to
`images/python-runtime/requirements.in`, run `make runtime-lock`, and name the library
in `run_python`'s description ([design/runner.md](design/runner.md)).

**The REST API.** Change `api/openapi.yaml` first. `tests/control/test_contract.py`
checks the contract, and the API's tests in `tests/control/` check every response
against it.

**The web client.** `make demo` serves the built client on a demo queue with sample
items, so a change can be tried without a stack. `make web` rebuilds the client, and a
restarted `make demo` serves the new build. The rules a client must keep, and the test
that holds each rule, are in [design/client-rules.md](design/client-rules.md).

**The images.** `make image-service` and `make image-runtime` build one image each.
`make images TAG=dev` builds both under another tag, and `make up TAG=dev` runs a
stack from them.

## Debugging a running stack

```sh
make status                          # containers, and the control plane's health
make logs SERVICE=runner             # one service's last 200 lines
make logs SERVICE=gateway FOLLOW=1   # follow one service
make restart SERVICE=broker          # after editing config/
make bridge-fg                       # the bridge in the foreground, to watch it
make shell SERVICE=broker            # a shell inside a container
```

| To learn | Look at |
|---|---|
| which sessions began and ended, and for which principal and app | the gateway's log |
| which servers and how many stubs the broker loaded | the broker's log, at start |
| why every mount is refused | the runner's log, which names the unusable line of your never-mount list |
| what a run called | the last line of the `run_python` result, or the run log |
| what became of a decision | the decided item in the web client, which shows the outcome |

The run log is a SQLite database on the volume `maji-run-log`, at
`/var/lib/maji/runs.db` in the broker's container. The run log holds names, outcomes
and sizes, never arguments or results:

```sh
make shell SERVICE=broker
python -c "import sqlite3; [print(r) for r in sqlite3.connect('file:/var/lib/maji/runs.db?mode=ro', uri=True).execute('SELECT started_at, principal, exit_code, duration_ms FROM runs ORDER BY started_at DESC LIMIT 10')]"
```

An executor is removed when its run ends. To see what a script can see, run a script
that prints it: `run_python` with `print(maji.mounts())`, or `list_api`.

## Conventions

[../AGENTS.md](../AGENTS.md) states these rules in full, with examples, for people
and for coding agents.

- **Python 3.14, managed with uv.** Ruff for lint and format, mypy for types.
- **Type hints on every function signature.**
- **Google-style docstrings,** with single backticks for code.
- **A comment explains a mechanism and what the mechanism achieves.** A comment never
  restates the code.
- **Dependencies resolve from public registries only,** and every lock file pins
  exact versions.
- **Your data stays out of the repository.** Tests use synthetic fixtures. Backups,
  measurement results and the secrets directory live outside the tree or are ignored.
- **A design change is a document change.** The commit that changes behaviour changes
  the design document, and a decision is recorded in [decisions.md](decisions.md).

## Technology

| Choice | Why |
|---|---|
| MCP Python SDK 2.x | The gateway is an `MCPServer` over streamable HTTP. The broker and the relay are MCP clients. |
| Starlette | The control plane's REST API. Starlette is minimal and comes with the MCP SDK. |
| SQLite | The run log and the control plane's database. One writer each, at one person's pace. |
| JSON-RPC 2.0 over Unix sockets | Language-neutral, so a client in any runtime needs only a socket and a JSON library. |
| Docker SDK for Python | The runner's client for the Docker API. |
| py_webauthn | Passkey verification. |
| Vue 3, TypeScript, Vite, Pinia | The web client, with types generated from the contract. |
| Playwright | The browser test, in your installed Chrome. |
| pytest, pytest-asyncio | The Python tests. pytest-asyncio also gives a test a free port: `unused_tcp_port`. |
| Faker, factory_boy | Made-up values and structures for tests, seeded for each test. |
| pytest-check | Checks that report every wrong fact about a result in one run. |
| pytest-cov | Coverage: `uv run pytest --cov=src/maji_mcp --cov-branch`. |
