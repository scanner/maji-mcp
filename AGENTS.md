# Working in this repository

The rules for changing maji-mcp, for people and for coding agents. `CLAUDE.md`
imports this file, so there is one copy of the rules.

maji-mcp is a self-hosted MCP service: an agent writes a script against typed
functions, the script runs in a sandbox, and a broker that holds the credentials
checks every call. Read these before a change:

- [README.md](README.md): what maji-mcp is for
- [docs/development.md](docs/development.md): the layout, the tests, and how to debug
- [docs/design/architecture.md](docs/design/architecture.md): the components and the
  trust boundaries
- [docs/design/glossary.md](docs/design/glossary.md): the terms, with one meaning each

## Commands

Run every command from the root of the checkout.

| To | Run |
|---|---|
| install the Python and web dependencies | `make install` |
| lint, and check formatting and types | `make check` |
| fix what lint can fix, and format | `make format` |
| run the tests that need only Python | `make test` |
| check the web client's types and run its tests | `make web-check` |
| run the tests that start containers, on `:test` images | `make test-docker` |
| drive the web client in Chrome | `make test-browser` |
| run all of the above | `make test-all` |

## What a finished change includes

- **`make check` and `make test` pass.** A change to `web/` or `api/openapi.yaml`
  also passes `make web-check`. A change to the runner, the sandbox, `compose.yaml`
  or an image also passes `make test-docker`.
- **The design document changes in the same commit.** Built behaviour is described in
  `docs/design/`. A design that is not built lives in `docs/design/planned/`, and
  moves when it is built.
- **`docs/roadmap.md` is ticked,** and a choice between alternatives is recorded in
  `docs/decisions.md` under the day's date.
- **A new term goes into the glossary.**
- **The README's tables change** when a build step, an install location or an output
  path changes.
- **`CHANGELOG.md` gains a line** under "Unreleased" for anything a user would notice.

## Boundaries

- **A stack may be running on this machine, and it is its owner's.** Without being
  asked, do not run `make up`, `make down`, `make restart`, `make bridge`,
  `make token`, or any `make connect-…` or `make reconnect-…`: they restart
  services, replace tokens, or edit an app's settings. Reading is fine: `make status`,
  `make logs`, `docker ps`.
- **Tests never touch a running stack.** Docker tests build and run `:test` images
  with their own volumes, ports and secrets. Never rebuild or remove `:latest`
  outside `make up`.
- **Secrets are never read into a conversation or a file.** They live in
  `~/.config/maji-mcp/secrets`. Compare a hash or a file mode when a check needs one.
- **Real data stays out of the repository.** Examples, fixtures, mockups and
  documents use made-up names: `example.com`, `ABC-123`, `someone`. Never copy a
  project key, a hostname, a person's name or a result from the machine you work on.
- **Lock files resolve from public registries only:** PyPI and npm. `make runtime-lock`
  pins the runtime image's libraries.
- **A trust boundary moves only on purpose.** The executor is untrusted and has no
  network and no credentials. Only the broker holds a service's key. Only a passkey
  decides an item. A change to any of these needs an entry in `docs/decisions.md` and
  a change to `docs/design/security.md`.

## Python

- **Python 3.14, managed with uv.** Ruff lints and formats, and mypy checks types.
- **Type hints on every function signature.**
- **Imports in three groups,** each under its comment: `# system imports`,
  `# 3rd party imports`, `# project imports`.
- **Every module opens with a docstring** that says what the module does and why. The
  module docstrings are the detailed design.
- **Anything a program writes by default has a path tied to the project or set by a
  declared setting,** never a path relative to the current directory.

### Docstrings

Google style, with `Args:`, `Returns:` and `Raises:` where each adds something. Code
goes in single backticks, never double.

```python
def secrets_in(path: Path, deadline: float, max_entries: int = MAX_ENTRIES) -> Found:
    """
    Look through a path for files named the way secrets are.

    Args:
        path: the folder or file a run asked to mount
        deadline: when the look must stop, as `time.monotonic()` counts
        max_entries: how many entries the look may list before it stops

    Returns:
        The files to cover, and whether the look was complete. A look that was
        not complete refuses the mount.
    """
```

- **Open with what the thing does.** Exclusions and caveats go in `Returns:` or
  `Note:`.
- **Describe the code as it is.** Leave out what it used to do, and arguments against
  designs nobody proposed.
- **Use one sense for each word in a file,** and the glossary's sense where it has
  one.

### Comments

Comment a mechanism a competent reader would not guess: a workaround for a library, a
security decision, an uncommon pattern. Do not comment what the code already says.

A comment names the mechanism, then says what the code achieves. It carries no
importance labels ("critical", "do not remove") and no story of what used to go
wrong.

```python
# Too much: a label, and a failure story
# CRITICAL: without `|| [ -n "$path" ]` the LAST file is silently skipped!

# Better: the mechanism, then what it achieves
# `read` needs a newline to end a line, so `|| [ -n "$path" ]` keeps the final
# field, which has none. A change to one file is then included.
#
```

A comment block of more than one line ends with a line holding only `#`, directly
above the code it describes. A blank line comes before a block that starts a new step.

```python
    meta = {"maji-mcp/principal": ctx.principal, "maji-mcp/run_id": ctx.run_id}

    # The SDK's client runs in task groups, so a failure arrives wrapped in
    # exception groups, which `except*` looks inside.
    #
    try:
        result = await session.call_tool(tool, args, meta=meta)
    except* OSError as group:
        raise MajiError("unavailable", "the server can't be reached") from None
```

## Tests

- **The tests mirror the package:** `src/maji_mcp/runner/mounts.py` is tested in
  `tests/runner/test_mounts.py`.
- **Cases of one shape are one test,** with `pytest.mark.parametrize` and an id for
  each case.
- **Made-up values come from the `faker` fixture, and repeated structures from a
  factory.** A literal stays where the value is the point of the test.
- **Independent facts are checked independently,** each in its own `with check:`
  block.
- **Every fixture has a docstring** that says what the fixture gives a test.

**Every test has a docstring in Gherkin form, and the Gherkin describes what the code
under test guarantees.** GIVEN is the situation the test stands for, WHEN is the
call, and THEN is the behaviour a reader can rely on. Parameters, patched constants
and fixtures are how the test is built: they go in a comment in the body, beside the
code that sets them up. Name what the code names, such as `MAX_ENTRIES`, so a reader
can search for it.

```python
# Describes the test harness
"""
GIVEN: A folder from the parametrized case, looked through with max_entries=5
 WHEN: secrets_in runs
 THEN: complete is False iff expect_refused
"""

# Describes the behaviour
"""
GIVEN: A folder an agent asked to mount
 WHEN: the runner looks through the folder for files named like secrets
 THEN: If the folder holds more than MAX_ENTRIES entries, the look says it is
       not complete, which refuses the mount
  AND: If the folder holds MAX_ENTRIES entries or fewer, the look is complete
"""

# ... and in the body:

# The real limit is far too large for a readable fixture, so this look may list
# 5 entries.
#
found = secrets_in(folder, deadline, max_entries=5)
```

## Documents

[docs/README.md](docs/README.md) lists every document and states these rules:

- **One status for each document.** `docs/design/` describes what is built, and
  `docs/design/planned/` what is not. A document says which in its first lines.
- **One meaning for each term,** as the glossary defines it.
- **A sentence names its subject.** Write "the broker checks the policy", and repeat
  "the broker" in the next sentence, so no reader has to look back for "it".
- **"You" is the person who runs the instance.** Text written for agents calls the
  same person "the user".
- **Present tense for what is built, "will" for what is planned.**
- **A statement about behaviour names the file that holds the behaviour.**
- **Say what a thing does,** and leave out what it used to do.
- **Short paragraphs, and lists whose items start with the point in bold.**

A README is written for a reader with none of the author's context. It says what the
project is for in its first paragraph, which platforms are supported, tested and
merely expected to work, what each command produces and where, and how to run what
was built, with one complete command for each program.
