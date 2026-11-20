# Evaluation: does the concept hold up?

**Status:** a record, written before anything was built (early November 2026) and
kept as written. The measurements the evaluation asks for were made
([measurements.md](measurements.md)), and the decision was to build. Two things have
changed since: a kept skill needs no review, and the work is ordered by
[the roadmap](../roadmap.md), not by the stages named here.

The design grew one idea at a time. This document steps back and asks whether the whole
thing does what it is for, using the tasks it would actually be used for.

**Verdict.** The concept holds up for three kinds of work: aggregation over large result
sets, joins across sources, and procedures that are worth doing the same way every time.
Outside those three it adds cost rather than removing it. Neither source article
publishes a measurement anyone can reproduce, so the first step is to measure it on your
own tasks before building the broker (see "A staged plan").

## The goal

- The agent and its harness spend fewer tokens on menial work: counts, targeted
  searches, arithmetic over sets, joins.
- The agent works with entities and questions, not one raw tool call at a time.
- Credentials and authority stay out of the agent's reach.
- The agent uses an existing skill first and writes code when none fits. Code that proves
  generally useful becomes a skill.

## How it would actually be used

| Task | Kind | Without maji-mcp | With maji-mcp |
|---|---|---|---|
| Which errors are new since release `2026.11.3`, grouped by top frame, across the `api` and `worker` projects? | Aggregation | Pages of Sentry events read into context | A skill returns about ten lines |
| Which tasks in a project have open checklist items, and how many closed this week? | Aggregation | The #20 problem: many list calls, or awk over `tasks.org` | One script over the parser's `Task` objects |
| Today's daily summary when there are no journal entries: commits across repos, PRs and closed tasks | Procedure across three sources | The `daily-summary` command runs `git log` in each repo under `~/src/`, then `gh pr list` and `list_tasks`, and reads every result in full | A skill gathers and condenses the activity. The model still writes the bullets, because that part is judgment |
| Jira tickets closed this sprint whose PRs touched files that now appear in new Sentry stack traces | Join across three sources | Dozens of calls, with all three sets held in context to join them | A script joins them and prints the matches |
| Is the fix for a given ticket on integration yet? | Short procedure | Four or five calls with small results | About even; a skill helps only if it is asked often |

The join is the strongest case. Without code, the model is the join engine and holds
every set in context. With code, it never sees the sets at all.

A typical session would go like this:
1. The agent searches the skill library for "sentry regression" and finds
   `top_frames_by_release`.
2. It calls the skill and gets eight lines back.
3. It needs a variant split by environment, so it writes a ten-line draft that calls the
   same skill and one stub.
4. The draft is useful, so the agent saves it as a candidate.
5. The candidate goes into the review queue (see [control-plane.md](../design/control-plane.md)).
   The agent carries on without waiting.

## Where it doesn't help

- **Single lookups.** Fetching one event or one task is one call either way.
- **Content the model has to read anyway.** Debugging a particular stack trace needs the
  trace itself in context. Code mode cuts menial tokens, not reasoning tokens.
- **Small intermediate results.** A script is output tokens, which are slower and cost
  more per token than input. A run that fails costs a round of reading the traceback and
  rewriting. Code wins when the intermediate data is much larger than the script.

This makes skills the most valuable part of the design. A skill call is one short call
with a short result, with no script for the agent to write and no debugging. The agent's
guidance follows from it:
- use a direct tool for one call with a small result
- write a script to chain, loop, filter or count
- use a skill whenever one fits

## Four things that would undermine it

**1. Tools that return text.** Code needs fields to group and join on. Most MCP servers
return formatted text for a model to read. emacs-org is one of them: every tool returns
`TextContent` lines, even though the MCP SDK it pins (1.25.0) supports structured tool
output (`outputSchema` and `structuredContent`). A stub over a text tool hands the
script a string to parse, which brings back the #20 awk problem inside the sandbox.
- For your own servers, return structured output alongside the text, or let the broker
  import the library directly.
- For third-party services, prefer their REST APIs, which already return structured data.

**2. Silent wrong answers.** A script can run cleanly, filter on a field it guessed
wrong, and report "0 events", and the model now sees less of the evidence than before.
- Stubs return typed records and raise on unknown fields and unknown filter keys, so a
  wrong guess is an error, not an empty result.
- Scripts print how many records they scanned and a small sample next to the answer.
- Promoted skills carry tests against fixtures, so a skill is code that has been shown
  to work.

**3. The agent harness has a shell.** Claude Code runs on the same machine as
maji-mcp, as your user, with Bash, and with claude-code-ide's `executeCode` into your
Emacs. Anything your user can read, the agent can read: a vault token in your
environment, a broker config file, the control plane's database. Keeping maji-mcp's
own secrets and decisions out of the agent's reach needs its services under an owner the
agent's shell can't use. The default deployment doesn't do that, because your user owns
the Docker daemon. Agent-written code is still sandboxed, and the hardening options in
[deployment.md](../design/deployment.md) close the rest for those who want it. Either way, what Claude Code can
already do stays unchanged. The same reasoning is why promotion is a signature
rather than a flag in a synced file (see [planned/sharing.md](../design/planned/sharing.md)).

**4. The skill library becomes the new tool-definition problem.** A few hundred skills
listed up front cost what a few hundred tools do.
- The agent finds skills by search, never by reading a full listing.
- Saving a candidate first searches for near-duplicates.
- Usage metrics retire skills nobody calls.

## What maji-mcp adds over Claude Code as it is

Claude Code already has skills that bundle scripts, a shell to run them, and tools that
load only when needed. That covers part of this design. What it lacks:
- **MCP from code.** A skill's script cannot call the MCP servers Claude Code is
  connected to. It needs its own clients and its own credentials.
- **Credentials out of reach.** Scripts run as you, with whatever you can read.
- **Governance.** There is no RBAC per call, no queue for approvals, and no promotion
  step.
- **Portability.** One library works from any harness, on any machine.

Where none of these four matter, a Claude Code skill with a script is the simpler tool.
Stage 0 below uses exactly that to test the idea cheaply.

## Abstraction across sources

The aim is to ask similar questions of several sources: issues in Jira, GitHub and
Sentry, documents in Confluence and Google Docs. A universal entity model designed up
front tends to end up as the lowest common denominator of its sources, and leaks
anyway. Start from thin stubs, one per source, that keep each source's own fields. Add a
normalized type, such as an `Issue` with `source`, `id`, `title`, `status`, `url` and
`updated`, when a promoted skill needs one. The shared types then come from the joins
you actually run.

## Other languages

Python runs the system. Skills can be written in any language the executor can build and
run, because each run is its own process. [languages.md](../design/planned/languages.md) covers what that
takes:
- sandboxed, offline builds
- separate build and run images per runtime
- per-language promotion policy
- a runner for calls between skills
- typed stubs generated from the broker's schemas

## A staged plan

The work is staged, with each stage useful on its own:
- **Stage 0:** measure, before building anything
- **Stage 1:** the core, read-only
- **Stage 2:** the control plane
- **Stage 3:** trust and sharing
- **Stage 4:** other languages and the live Emacs

The concrete items, and progress on them, are in
[the roadmap](../roadmap.md). If stage 0's numbers don't move, the
broker isn't worth building.
