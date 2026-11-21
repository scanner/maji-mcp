# Changelog

What changed in each release of maji-mcp, for the people who run it. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version
is 0.x, any release may change an interface.

## [Unreleased]

### Added

- **The released images name their source repository,** so ghcr.io lists them with
  maji-mcp, with a description and the license.

## [0.1.0] - 2026-11-20

The first public version. Scripts only read: the first work is read-only on purpose,
to get the patterns right before a script is allowed to change anything. Write
access is planned.

### Added

- **Scripts in a sandbox.** `run_python` runs an agent's script in a fresh container
  with no network and no credentials. A script keeps values for the session's later
  scripts, makes independent calls at once, and imports libraries for code, text and
  data.
- **Typed access to services.** Sentry, Jira and GitHub over their REST APIs, and any
  stdio MCP server that acts as you, such as emacs-org-mcp, through a bridge.
- **Connections.** A service's key is pasted into the web app, and only the broker
  keeps it.
- **Mounts.** A script reads the folders you grant, read-only. A project is one
  grant, files named like secrets read as empty, and the paths on your never-mount
  list are never mounted. Helpers read git history and SQLite databases.
- **Skills.** An agent keeps a function it wrote and ran, and calls the function from
  later scripts. You promote a skill to share it with your other agents, and demote
  a skill to stop it.
- **A queue for decisions, with a web client.** Requests to mount paths, promote a
  skill or connect a service wait for you. Decisions are signed with a passkey, and a
  grant is remembered for a session, until a time, or until you revoke it.
- **A policy, rate limits and a run log** for every call a script makes.
- **Apps.** Claude Code through a headers helper, and Claude Desktop through a relay.
- **Released images** on ghcr.io, `ghcr.io/scanner/maji-service` and
  `ghcr.io/scanner/maji-python-runtime`, for linux/amd64 and linux/arm64.
- **Commands** to start the stack, connect an app, run the bridge, try a demo queue,
  and back up and restore the queue's database.

[Unreleased]: https://github.com/scanner/maji-mcp/compare/0.1.0...HEAD
[0.1.0]: https://github.com/scanner/maji-mcp/releases/tag/0.1.0
