# Planned: connections

What remains of connecting maji-mcp to services: signing in to remote MCP servers with
OAuth, disconnecting by service, and logs from cloud storage. A **connection** is
maji-mcp's own credential for one service.

**Status:** planned, and the next work. Connections by API key are built: you paste
the key into the web app, and only the broker keeps it ([../connections.md](../connections.md)).
Sentry, Jira and GitHub are connected that way.

## Remote MCP servers that use OAuth

Most services that offer an MCP server, such as Sentry's and Atlassian's, sign a client
in with OAuth. The broker will be the OAuth client, through the MCP Python SDK's
`OAuthClientProvider`, with three hooks of maji-mcp's own:

- **The redirect handler** receives the provider's sign-in address and raises the
  "Connect <service>" item. Your passkey approves the connection before your browser
  is sent to the provider.
- **The callback** is the control plane's `/oauth/callback`. Its `state` is single-use
  and bound to the item you approved. The control plane passes the code to the broker
  over its socket. The PKCE verifier never leaves the broker.
- **The token storage** is the broker's credential store. Refresh tokens keep the
  connection working without asking you again.

What has to change in the broker for this:

- **Stubs from the configured tools, not from the server's tool list.** The broker
  builds an MCP server's stubs from `tools/list` when the broker starts. A server with
  no connection answers 401, so it would have no stubs, and no call could raise the
  Connect item.
- **Signing in in the background.** The SDK waits for the callback inside the request
  that needed it. A stub call fails at once instead, naming the item, as it does for a
  service with no key.
- **One session per server,** in place of a new MCP session for each call, which is
  slow for a server across the internet and under `maji.parallel`.
- **Client registration.** A client ID metadata document needs a public HTTPS address,
  which a self-hosted instance doesn't have. Until the project has one, the broker
  registers with each server dynamically, or uses an OAuth app you register by hand.
  On a remote host, the callback has to be reachable from your browser, behind the
  same TLS or private network as the gateway.
- **Read-only scopes** are asked for at the provider's consent, as well as the broker
  refusing tools that write.

**Sentry's MCP server becomes a second namespace,** beside the REST stubs, for the tools
the REST stubs don't cover, such as replays, traces, profiles and monitors. Sentry's
server supports both dynamic registration and client ID metadata documents. An access
token from Sentry's MCP server works only at that server, so it cannot feed the REST
stubs.

**Before relying on a server, check what a script gets from it.** The spike on Sentry's
server ([../../decisions.md](../../decisions.md), 2026-11-13) found each of these
mattered:

- whether tools return structured content, as well as declaring an output schema
- whether a list can be read to its end, with a cursor, or stops at a page
- what a count means under a filter
- how long a call takes, and whether a model runs inside it
- whether reads are cached, and how to bypass the cache

Atlassian's server needs this check before it becomes a namespace beside the `jira`
stubs, which read Jira's REST API with a key. The server's sign-in by API token needs an
administrator of your Atlassian organization to turn it on.

## OAuth for REST and GraphQL services

Not planned yet. A service with only a REST or GraphQL API takes an API key, which is
built. OAuth for such a service needs an OAuth app registered with the provider: either
you register one and paste its ID and secret, which is no easier than pasting a key, or
the project registers one for each provider. A service definition can declare OAuth
later without changing the credential store, the callback or the Connect item.

## Disconnecting by service

`DELETE /connections/{service}` in the contract will disconnect a service from the
Connections page directly, and revoke its tokens at the provider where the provider
supports that. Today Disconnect is offered on the Connect item whose key is in effect.

## Logs from cloud storage

Some logs are kept only in storage buckets, such as the levels a console does not
keep. Gigabytes cannot come back through a stub's 16 MB message, and a later script
should not download the same objects again.

- A connection whose read-only scope names the buckets and prefixes.
- The broker downloads objects into a cache volume, keyed by object and generation.
- The session's executors mount the cache volume read-only. This is a second source
  of mounts, beside granted paths ([../runner.md](../runner.md)).
- A script reads the objects like any mounted file, or loads them into SQLite.

[connectors.md](connectors.md) designs the cache and the copying: a fetcher container
running maji-mcp's own image copies the objects, and the broker keeps the cache.

## Building it

In order:

1. OAuth sign-in for remote MCP servers, with the control plane's callback route.
2. Sentry's MCP server as a second namespace.
3. Atlassian's MCP server checked, for a namespace beside the `jira` stubs.
4. The measured tasks that need Jira and GitHub, run through `run_python`
   ([../../research/measurements.md](../../research/measurements.md)).
5. Disconnecting by service.
6. Cloud storage, with the cache volume.
