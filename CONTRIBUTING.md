# Contributing to maji-mcp

maji-mcp is early, and parts of its design can still change. This page says how to
propose a change so that it fits the direction the project is taking.

## Before you start

- **Read what is built and what is planned.** [docs/roadmap.md](docs/roadmap.md) orders
  the work. A document in `docs/design/` describes the running system, and a document
  in `docs/design/planned/` describes a design that is not built.
- **Talk about a design change first.** Open
  [an issue](https://github.com/scanner/maji-mcp/issues) before you change a trust
  boundary, an interface, or anything a planned document already designs.
  [docs/decisions.md](docs/decisions.md) records why each choice so far was made.
- **Check your platform.** maji-mcp is developed and tested on macOS with Docker.
  Linux does not work yet, and making it work is welcome
  ([docs/roadmap.md](docs/roadmap.md), "Going public").

## Making a change

```sh
make install     # the Python environment and the web client's dependencies
make check       # lint, formatting and types
make test        # the tests that need only Python
```

[docs/development.md](docs/development.md) covers the layout, the other test suites,
and how to debug a running stack. [AGENTS.md](AGENTS.md) states the rules for code,
comments, tests and documents, and what a finished change includes. The rules apply
to people and to coding agents alike.

In short, a change is ready when:

- `make check` and `make test` pass, with `make web-check` or `make test-docker`
  where the change touches the web client, the REST contract, the sandbox or an image
- the design document, the roadmap and the changelog say what the change does
- every example and fixture uses made-up names and data

## Commit messages

The subject line says what maji-mcp now does, as a sentence a user could read:
"Mount a project as one grant, with its likely secrets read as empty". The body says
why, and what a reader of the code would not guess.

## Security

Do not open a public issue for a vulnerability. [SECURITY.md](SECURITY.md) says how to
report one.

## License

maji-mcp is released under the BSD 3-Clause License ([LICENSE](LICENSE)). A
contribution is offered under the same license.
