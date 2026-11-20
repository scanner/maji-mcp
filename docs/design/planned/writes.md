# Planned: writes as changesets

How a script will change something: a task in emacs-org, a Jira ticket, a branch in a
repository. A write will be recorded as a changeset, wait in the queue, and be applied
after your decision.

**Status:** planned. Today the broker refuses every stub classified as a write
([../broker.md](../broker.md)). The control plane already stores, shows and decides
items of the kind `changeset`, and `make demo` shows samples. The broker's side is
not built.

## Why a write cannot simply wait

A queue answers at human speed. A script cannot wait inside a write call for an
answer: the script would time out and the session would stall. A write is therefore
planned in one step and applied in a later step.

1. A script calls a write stub, such as `org.update_task(...)`.
2. The broker records the proposed change and a **precondition**: the version of the
   target as the script read the target. The call returns a changeset id.
3. The script finishes and prints its result, changeset ids included. The agent
   carries on with other work.
4. You approve the changeset in the queue.
5. The broker checks the precondition again and applies the change.
6. If the target has changed since the script read it, the changeset goes back to the
   agent as a conflict, and nothing is overwritten.

Consequences:

- **Pending means pending.** A script whose next step needs a write already applied is
  split into two runs, with the approval between them.
- **An approved changeset is applied even when its session has ended.** The outcome is
  in the history, and in the answer to the agent's status request.
- **The precondition is the version the script read,** never a fresh read at record
  time. A fresh read would hide a change made between the script's read and the
  broker's record ([server-contract.md](server-contract.md), A4).
- **A timed-out write has an unknown outcome.** The broker never retries blind. A
  retry carries the same precondition, so a write that already landed comes back as
  a conflict (A5).
- **A cancelled call can cut a write off partway.** MCP SDK 2.x passes a cancelled
  call on to the server, so applying a changeset has to allow for a write that was
  interrupted.

## What needs your decision

**The baseline: approval is for content a person might want to edit before it lands.**
A structural change you already asked for needs no approval.

- **What you asked for is approval enough when the broker can check it.** A tick you
  asked for is applied at once: the broker checks that the change is exactly one
  checkbox's state.
- **Content the requester wrote comes to the queue:** a journal entry, a task body, a
  ticket's text. The item shows the broker's diff beside the agent's description.
- **Content the requester wrote is never remembered,** for now. Patterns cover reads,
  mounts and structural changes.
- **Reads within the policy need nothing.**

## Policy rules

A **policy rule** will decide a low-risk kind of item without you. Policy rules live
in the control plane and are separate from the broker's policy file.

- **Policy v0:** approve structural changes you already asked for, and a tick the
  broker has checked.
- **A change to a policy rule is itself a queue item.**
- **Approval-rate metrics propose policy rules.** When you approve one kind of item
  every time without opening it, the control plane proposes a rule, as an item.
  Rubber-stamping then shows up as a decision you can see and revisit.
- **A decided item says which rule decided it.** The web client already shows this
  for the demo's sample.

## Keeping the queue short

- **Similar items are coalesced.** Five updates to the same journal entry in one
  session become one item that shows the final diff.
- **A precondition can cover only the sections a change touches.** A typical task
  update changes 3 of a task's 8 sections
  ([../../research/measurements.md](../../research/measurements.md)). With a
  precondition for each section, two pending changes to different sections of one
  task do not conflict, and the diff shows only the changed sections.

## emacs-org

emacs-org-mcp is used as it is. Writes go through emacs-org-mcp's own tools, so its
write guard, its `:MODIFIED:` stamp and its git commits all still apply.

- **emacs-org-mcp has no `if_match`.** The write stub will read the target again just
  before writing, and compare the result with what the script read. A changed target
  comes back as a conflict.
- **The queue takes the place of ediff approval** for writes the broker applies. Once
  the queue applies org writes, `make bridge` will run emacs-org-mcp with
  `--no-ediff-approval`, a flag emacs-org-mcp already has. Until then ediff stays on,
  because ediff is the only check on a write that reaches the bridge.
- **An app's own emacs-org connection is a separate process** and keeps ediff.
- **Ediff remains a way to view a changeset,** from the planned Emacs client.

### The first skills that write

Checklist-item operations over emacs-org-mcp's existing tools. Each operation matches
exactly one item, or fails listing the candidates, and keeps the `[n/m]` progress
cookie right:

- `complete_task_item`: find the item by its text, check it, and recompute the
  section's progress cookie. The code exists, in `tests/local/org_skills.py`, and
  `make test-local` runs it against a real emacs-org-mcp. The first measurement's
  follow-up measured it
  ([../../research/measurements.md](../../research/measurements.md), "Once the code
  exists").
- `update_task_item`, `add_task_item`, `delete_task_item`, `add_task_link`
- a section edit: replace or append to named sections, add sections, and check off or
  add items, in one call. Sending only the changed sections would send 43% of what
  `update_task` takes. Most ticks come inside such an update: 118 of the 120 commits
  that checked off an item changed other lines too.

The task's full body passes through the script, never through the model.

## Repositories

A real repository is never mounted writable: code that can write `.git/hooks` or
`.git/config` runs as you the next time you use git there.

- A script works in a copy inside its run.
- The run's writes come back as a git bundle, a file the runner fetches from. Applying
  the writes never runs git inside the run's copy, whose config the script could
  write.
- Once approved, the writes land as a branch or a worktree.

## Building it

In order:

1. Write stubs record a changeset, with the version the script read as the
   precondition, and return the changeset id.
2. The broker computes the item's facts: the diff, the stubs called, the size.
3. The broker applies an approved changeset through the decision feed that already
   applies promotions, re-checking the precondition, and reports an outcome.
4. The re-read precondition for servers without `if_match`.
5. Retry rules for a timed-out write.
6. Policy v0, then coalescing.
7. The bridge's emacs-org-mcp runs with `--no-ediff-approval`.
8. The first skills that write, promoted through the queue as high-risk items.

**Done when:** an agent's emacs-org edit is applied after approval in the web client
and appears in the history, and a stale changeset comes back to the agent as a
conflict.
