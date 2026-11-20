# Planned: seeing the skill library

A page of the web client that shows every skill agents have kept, promoted or
imported, so you can see what code runs on your behalf.

**Status:** planned. Today you see a skill only when an agent asks you to promote it
([../skills.md](../skills.md)). The contract reserves routes for the library
(`api/openapi.yaml`), and none is built.

## Why

A kept version is code an agent wrote and reuses, and nobody has reviewed a kept
version. Keeping needs no decision, so the library grows without anything reaching
your queue. You need a place to see what agents have kept, what you have promoted,
what came from elsewhere, and what you could share.

## What the page shows

- **Every skill, with every version and each version's state:** kept, waiting on a
  promote item, promoted, demoted, changes requested or rejected, with your note.
- **For a kept version:** the principal that kept it, when, from which session and
  run, and what that run called. A kept version is marked as not reviewed.
- **For any version:** the code, the summary agents read, and what the code reaches:
  the stubs it reads with, the stubs it writes with, the skills it calls and the
  modules it imports.
- **Use:** how often each skill has been called, and when last, from the run log.

A script that was never kept is not in the library. The run log holds only the hash
of a script, never the script.

## Grouping

- **By area:** the namespaces a skill reaches, such as `sentry` or `org`, and skills
  that read only files. A skill that reaches two areas appears under both.
- **By state:** kept and not reviewed, waiting, promoted, demoted.
- **By origin:** kept on this instance, or imported in a pack
  ([sharing.md](sharing.md)).
- **By bundle:** the sets of skills you have marked to export together. A bundle
  becomes a pack through the export review.

## What you can do from the page

- **Promote a kept version** without waiting for an agent to ask. The promotion is
  the same signed decision as a decision on a promote item, with the same risk.
- **Demote** the promoted version.
- **Leave a note** agents see when they read the skill.
- **Later: mark skills for a bundle,** and start an export.

## How it will work

The library is on a volume only the broker mounts, and the control plane serves the
web client. The broker will report the library's index to the control plane over the
broker's socket whenever the library changes: names, versions, states, summaries,
reach and code. The control plane keeps the index as a copy to read from, and serves
it on the library routes.

- **No component gains a new way in.** The broker stays the control plane's client.
- **The control plane still decides nothing alone.** A promotion from the page goes
  through the same signed route as every decision, and reaches the broker through
  the same decision feed.

## Building it

In order:

1. The broker reports the library's index, and the control plane stores it.
2. Library routes in the contract and the REST API, read-only.
3. The page: the list, the groupings, a version's code and reach.
4. Promote and demote from the page.
5. Use counts from the run log.
6. Bundles, with export ([sharing.md](sharing.md)).
