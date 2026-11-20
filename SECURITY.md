# Security

maji-mcp exists to put a boundary between code an AI agent writes and your
credentials, your files and your decisions. A way through that boundary is a
vulnerability, and reports of one are welcome.

## Reporting a vulnerability

Report privately, through the repository's **Security** tab: choose **Report a
vulnerability**, or open
[a new advisory](https://github.com/scanner/maji-mcp/security/advisories/new)
directly. Do not open a public issue or a pull request for it.

Say what you can of:

- the boundary that fails, and what crosses it
- the steps, the script or the request that shows the failure
- the commit you tested, your platform and your Docker

You will get an acknowledgement, and then either a fix or an explanation of why the
report falls outside what maji-mcp protects.

## What maji-mcp protects

[docs/design/security.md](docs/design/security.md) states each protection, what it
rests on, and each limit. In brief, these are meant to hold:

- **A script cannot leave its sandbox.** An executor has no network and no
  credentials, and reads only the paths you granted.
- **A service's key stays with the broker.** A key never reaches a script, a result,
  an error, the run log, or the queue's database.
- **Only your passkey decides.** A token lets an agent ask, and never decide.
- **A mount leaves out what it says it leaves out:** files named like secrets, and
  the paths on your never-mount list.

## What is outside the boundary

These are stated limits, and a report of one is not a vulnerability:

- **A program running as you can reach the Docker daemon** in the default deployment,
  and through it the broker's credentials
  ([docs/design/deployment.md](docs/design/deployment.md)).
- **A mount covers files by name.** A token inside an ordinary file, or in a
  repository's git history, is readable by a script you granted the folder to.
- **An app keeps its own direct connections.** maji-mcp adds a way for agents to work,
  and removes none.

## Supported versions

maji-mcp has no releases yet. Fixes go to the main branch.
