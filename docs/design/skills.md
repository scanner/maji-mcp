# Skills

A skill is a Python function an agent wrote, tried in a script, and kept for reuse.
This document covers keeping a skill, promoting a skill, demoting a skill, and the
library that holds skills.

**Status:** this document describes what is built: a library on one instance.
Signatures, sharing across machines and packs are planned
([planned/sharing.md](planned/sharing.md)). Code: `src/maji_mcp/broker/skills.py`,
`library.py`, `skill_code.py` and `decisions.py`.

## Why skills

A script costs an agent output tokens to write, and a failed script costs a round of
reading the error and rewriting. A skill is a script's useful part, written once. A
skill call is one short line with a short result. The first measurements found a
small write 26% more expensive than a direct call when the agent had to work out the
code, and about 80% cheaper once the code existed
([../research/measurements.md](../research/measurements.md)).

## Three states of a version

| State | Who can run the version | How a version gets there |
|---|---|---|
| **kept** | the principal that kept the version, in any of the principal's sessions | an agent calls `save_skill` |
| **promoted** | every principal whose policy allows `skills.<name>` | you choose Promote and sign on a promote item |
| **demoted** | no principal, the principal that kept the version included | you choose Demote on a promotion that is in effect |

State decides what runs. The location of a file plays no part.

## Keeping a skill

An agent writes a function in a `run_python` script, calls the function there to see
the function work, and then calls `save_skill(name, summary, run=<run id>)`.

1. **The gateway finds the script.** The gateway keeps each session's last 20
   successful scripts, so the agent names the run and does not send the code again.
   An agent sends the code in `code` only for a run the gateway no longer has.
2. **The broker checks the run against the run log:** the same principal, the same
   script, and exit code 0.
3. **The broker takes the function out of the script** (`skill_code.extract`). The
   broker keeps imports, functions, classes, `UPPER_CASE` constants and the module
   docstring, each with the comment lines just above. The broker drops every other
   top-level statement, such as the lines that called the function and printed, and
   reports which lines were dropped.
4. **The broker checks that the kept code stands alone.** Every name the kept code
   reads must be defined in the kept code, built in, or one of the globals a skill is
   given: `maji`, `skills` and the namespaces. A function that leaned on a variable
   the script set is refused, naming the variable.
5. **The broker saves the code as a version** in the library, with a record of the
   save: the principal, the session, the run, the time, and what the run called.

The function must be named `name`, with typed parameters, a return type and a
docstring. The summary is one line of at most 200 characters. The summary is what an
agent reads in `list_api`.

**Keeping a skill needs no decision.** A skill runs with the caller's permissions:
every stub a skill calls is checked as a script's call would be. A kept skill
therefore gives an agent nothing a script does not. An agent refines a skill by
keeping the skill again, which makes a new version.

`list_api` shows a kept skill to the principal that kept the skill, marked "kept by
you, not reviewed". No other principal sees a kept skill.

## Calling a skill

A script calls `skills.<name>(...)`. The `maji` module asks the broker for the skill
over the call socket, with the run token. The broker answers with the skill's code
when:

- the policy allows the run's principal `skills.<name>`
- the principal is within the rate limit
- the library has a version the principal may load

The code then runs in the script's own process, with the script's own permissions.

The version a principal loads is the later of two: the last version the principal
kept, or the promoted version. A principal that kept a version before you promoted
another loads the promoted version. A principal that keeps a new version after the
promotion loads its own kept version. A principal that kept nothing loads the
promoted version.

## Promoting a skill

Promotion is for two cases: you want a skill shared with other principals, or the
skill writes and you should review what the skill reaches before the skill is trusted.
A skill only its keeper uses needs no promotion.

An agent calls `promote_skill(name, why=...)`. The broker raises an item of the kind
`promote`. The item shows:

- **the code,** as a new file, or as a diff from the version promoted now
- **the summary** every agent will read
- **what the skill reaches,** read from the code without running it
  (`skill_code.analyse`): the stubs the skill reads with, the stubs the skill writes
  with, the skills the skill calls, and the modules the skill imports
- **anything the skill reaches by a name worked out at run time,** flagged, because
  reading the code cannot follow such a name
- **what the run the skill came from called,** flagged where the code does not name
  the call
- **the agent's reason,** as the claim

The item names the skill and the version in `skill`, so the control plane can tell
which promotion is in effect for a skill.

### What sets the risk

The risk of a promotion follows from what the skill reaches, never from the skill
being code.

- **A skill that writes, or whose reach cannot all be read from the code,** is high
  risk, with the reason `trust_changes`: once promoted, any allowed principal may run
  the skill. The decision takes your passkey.
- **A skill that only reads** is an ordinary item. Any principal the skill is shared
  with could make the same reads in a script. The item can be decided in a batch.

### What you can decide

| Action | Effect |
|---|---|
| **Promote and sign** | The version becomes the promoted version of the skill. |
| **Request changes** | Nothing is promoted. The agent sees your note when the agent reads the skill, keeps the skill again, or asks again. A note is required. |
| **Reject** | Nothing is promoted. The agent sees your note, if you left one. |

Asking again replaces the item still waiting, so the queue holds one item for each
skill and principal.

A note reaches an agent as one line of printable text, whichever decision the note
came with. The broker runs the note's line breaks and spaces together and leaves out
control characters, such as a terminal's escapes. A note therefore cannot pass for a
line of the skill's own description. The control plane keeps the note as you wrote it.

### How a decision reaches the broker

The control plane records your decision. The broker reads decisions through the
decision feed (`src/maji_mcp/broker/decisions.py`):

- The broker reads the decisions after the last position the broker applied, in the
  order you made them, and keeps the new position in a file. A restart neither
  misses a decision nor applies a decision twice.
- The broker reads the feed every few seconds, and again whenever something is about
  to depend on the feed, such as a script calling a skill. A decision you have just
  made holds from the next call on.
- The broker reports an outcome for each decision: applied, conflict or failed.

A promotion is applied only when the version's files and the skill's promoted version
are still as they were when the item was raised. A promotion overtaken by another
promotion, or whose files changed, is a conflict.

## Demoting a skill

A decided promotion offers **Demote** while the promotion is the one in effect for
the skill: no later promotion of the same skill has been decided, other than one that
failed to apply.

- **A demotion is a new decision on the same item.** The demotion names the promotion
  in `supersedes`. The item stays decided, says what the demotion took back, and
  moves to the top of the history.
- **A demotion carries a note at most.** A demotion only narrows what agents can do,
  so an unlocked tab signs a demotion without a passkey prompt, even on a high-risk
  promotion. The control plane refuses a demotion that carries anything else.
- **The demoted version runs for no principal,** the principal that kept the version
  included. All of your agents may share one principal, so a demotion that left the
  keeper its version would change nothing.
- **Each agent that tries the version is told** that you demoted the version, when,
  and your note. `read_api` says the same. `promote_skill` refuses a demoted version,
  and keeping the same code again reports the version as demoted.
- **An agent can keep a changed version,** which runs as the agent's own kept
  version.
- **A promotion waiting to replace a version you have since demoted still applies.**
- **Demoting a version that is no longer the promoted version is a conflict.**

## The library

The library is a directory tree on a volume only the broker mounts
(`src/maji_mcp/broker/library.py`):

```
<publisher>/<pack>/<name>/
    <version>/skill.py        the code
    <version>/manifest.json   the function, signature, summary, and what the code
                              calls and imports
    <version>/SKILL.md        the same, written for people
    <version>/saves.json      each time the version was kept: principal, session,
                              run, time, and what the run called
    <version>/review.json     the item that asked you to promote the version, and
                              what became of the item
    promoted.json             the promoted version, and your decision
    revoked.json              the versions you demoted
```

- **A version is named by a hash of the code and the manifest together,** shortened
  to 16 hex characters. Changing either makes a new version, a change to the summary
  included. The same code kept twice is one version kept twice.
- **The publisher comes from the instance's configuration** (`MAJI_PUBLISHER`, default
  `me`). Every skill is in the pack `local`.
- **Each file is written whole and moved into place,** so a reader never sees half a
  file.
- **A promoted version must still hash to what you promoted,** or the broker does not
  load the version.

## What a promotion rests on today

A promotion is your signed decision in the control plane's database. The broker
applies the decision and does not yet verify the signature. Until signature checks
are built, the boundary is the volume: only the broker mounts the library.

## Planned

- **Signatures the broker verifies** before loading a promoted version, and signed
  demotions, so a library can be synced between machines
  ([planned/sharing.md](planned/sharing.md)).
- **A run policy for each instance:** which versions may run there.
- **Packs:** versioned sets of skills shared between personas as source.
- **The first skills that write:** checklist-item edits over emacs-org-mcp's tools,
  and Jira tickets. These wait for changesets ([planned/writes.md](planned/writes.md)).
- **Skills as gateway tools of their own,** found by search.
- **A page that shows the library:** every kept, promoted and imported skill, with
  its code and reach ([planned/library-view.md](planned/library-view.md)).
- **Skills in other languages** ([planned/languages.md](planned/languages.md)).
- **Tests shipped with a skill,** on small synthetic fixtures.
