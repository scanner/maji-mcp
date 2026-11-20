# Agent guidance: recognizing when code is the better tool

maji-mcp helps only when an agent notices, mid-task, that the work would be simpler as
a few lines of code, or as a call to a skill that already exists. This document covers
the signals an agent can notice, an estimate of the saving, and how the guidance
reaches an agent.

**Status:** built. The gateway's instructions carry the signals and the estimate, and
the run log counts how scripts use the runtime libraries. Hooks, a Claude Code skill,
tool notes and proposals from the run log are designed in
[planned/agent-guidance.md](planned/agent-guidance.md).

Often the moment comes while the agent is using another server directly, such as
emacs-org's `update_task`, without consulting maji-mcp at all. The guidance therefore
has to reach the agent where the agent makes the choice.

## The signals

Each signal is something an agent can notice about its own next step. Each has a
code-shaped alternative.

| Signal | Example | Instead |
|---|---|---|
| **A small change to a large record.** The agent is about to regenerate a whole record to change part of it. | ticking one checklist item with `update_task`, which takes the full task body. Tasks that get ticked are large: a median of about 7,600 characters in the org history, and 16,800 at the 90th percentile | call `complete_task_item`, or a section edit, if one exists: the first measurements put a tick at about 80% less than regenerating, and the median update changes 3 of a task's 8 sections. If none exists and the edit will recur, write the code and keep it as a skill: it pays back within one to three reuses (see "Estimating the saving") |
| **The same tool, many times.** One call per item, page or id. | `get_issue_details` for each of 30 Sentry issues | one loop in a script |
| **Reading a lot to keep a little.** A large result read in full, to extract a count, a filter or a maximum. | listing 105 tasks to count those with open checklist items | filter and count in code, and print the number |
| **Copying between tools.** Output from one tool pasted into another tool's arguments. | Jira keys into GitHub searches, then file names into Sentry queries | a join in one script |
| **A sequence done before.** The agent is repeating steps it, or another agent, has done before. | assembling the daily summary from journal, git, PRs and tasks | search the skill library first |
| **Paging.** Following cursors or offsets by hand. | `offset=50`, `offset=100`, and so on | a loop in code |

Three signals point the other way, toward a direct call:
- **One call with a small result.**
- **A small write that won't recur, with no skill for it.** The first measurements
  found that working out new code for a single small edit cost 26% more than
  regenerating the record ([../research/measurements.md](../research/measurements.md)).
  Few edits are one-offs, though: items are checked off about 30 times a week in the
  org history.
- **Content the model has to read to judge,** such as the stack trace being debugged
  or the wording of a ticket.

## Estimating the saving

The signals say where code might help. To decide whether it will, an agent can estimate
both costs from things it can see, before doing either: how many characters it would
write and read, how many calls it would make, and how many turns are left. It needs no
experiment.

**The cost of one use,** in output tokens, with everything else counted at its price in
output tokens:

    cost ≈ 0.45·W + 0.04·R + 400·k + 0.1·(W + R)·(1 + t/25)

- **W:** the characters the agent writes. For `update_task`, the whole task. For a skill,
  its arguments. The first time in code, the script.
- **R:** every character of tool results that enters the context. That means the record
  itself, plus the guides, schemas, listings and source the agent reads along the way.
  For a script, only what it prints.
- **k:** the tool calls.
- **t:** the turns likely left in the session. Whatever is read or written now is read
  again, from the cache, on every later turn.

The first three terms are what the agent writes: the text itself, its reasoning about
what it reads, and its reasoning around each call. The last is the context it adds.

**When code pays:**

    saving per use       s  = cost(direct) − cost(skill)
    first time in code   c₀ = cost(writing and checking the code) − cost(direct)
    worth a skill when   n > c₀ ÷ s

**n** is how many times the operation will happen. Count the uses so far in the session,
look in the run log, or read the record's history, such as `git log` on the org files.

**Example: checking off an item in a 10,000-character task,** with the counts first
measured, and about 5 turns left:
- **Direct,** as the baseline runs did it: W = 10,150 and k = 6, and they read about
  75,000 characters, including the format guide and task listings. That gives
  4,568 + 3,000 + 2,400 + 10,200 ≈ 20,200, about $0.40.
- **With `complete_task_item`:** W ≈ 300, R ≈ 5,000 and k = 4, so about 2,600.
- **The first time in code:** W ≈ 2,500 for the script, R ≈ 128,000 (it read the helper's
  source and the server's), and k ≈ 10, so about 25,900.
- So s ≈ 17,600 and c₀ ≈ 5,700. Measured, after allowing for how warm the cache was, they
  were 17,550 and 4,150. c₀ ÷ s is under 1, so the code pays for itself on its first
  reuse. The baseline's two runs differed threefold, so at worst it pays back by the
  third.

**Where the constants come from.** They are for Claude Opus 5.5, from the first measurements
([../research/measurements.md](../research/measurements.md)):
- **0.45 per character written:** text costs its own tokens to write, and org text
  measured 2.3 characters per token.
- **0.04 per character read, and 200 output tokens per call:** fitted to the output
  tokens of 29 runs across all six tasks. For half of them, the prediction is within
  ×0.7 to ×1.3 of the actual output. The worst, on the smallest runs, are ×0.5 and ×2.2.
- **400 per call** is those 200, plus about 200 for re-reading a 20,000-token base
  context (system prompt and tools) from the cache on that turn. A larger context costs
  more per call.
- **0.1 per character in the context:** 1 ÷ 2.5 tokens (Python measured 2.7 characters
  per token), written to the cache at 1.25 times the input price, with input at a fifth
  of the output price.
- **t/25:** each later turn reads it from the cache at 0.05 times the input price, a
  twenty-fifth of the cache write. On Claude Sonnet 5.5, cache reads cost 0.1 times
  input, so use t/12.5.

The formula is an estimate for choosing between approaches, good to within a factor
of about two. Checking the constants against the run log, for each model, is planned
([planned/agent-guidance.md](planned/agent-guidance.md)).

## How the guidance reaches the agent

1. **The gateway's MCP instructions.** The gateway's `instructions`, sent when an app
   connects, carry the signals in brief and the cost estimate (`INSTRUCTIONS` in
   `src/maji_mcp/gateway/server.py`). Claude Code puts server instructions into the
   system prompt, so the agent sees the instructions before the agent's first tool
   call. Every app that honours `instructions` gets them.
2. **Tool descriptions.** `run_python` and `save_skill` describe how to use them at
   the moment of the call. A skill's summary is the line an agent reads in
   `list_api`, so a summary names what the skill does and when to use the skill.
3. **What each run called.** Every `run_python` result names the stubs and skills the
   run called, so the agent and you both see what a script used.
4. **What a script can import.** The instructions say in one line that a script can
   import runtime libraries for code, text and data, and `run_python`'s description
   names each one. `read_api("python")` lists them with what each is for, and
   `read_api("python.<name>")` gives a library's notes: where to start, and what to
   watch for, such as a YAML loader that runs code. `search_api` finds a library by
   its summary. For the rest, a script prints `help(...)` on what it needs, which
   documents the installed version. The notes are kept in
   `src/maji_mcp/common/runtime_libraries.py`.

## Whether agents need more about a runtime library

The notes are short on purpose, and `help()` covers the rest. The run log shows
whether that is enough (`src/maji_mcp/broker/runlog.py`):

- **Each run's use.** As a script ends, the executor tells the broker which runtime
  libraries the script imported, what it called `help()` on, and, if it failed, the
  exception's class and the library it was raised in (`src/maji_mcp/executor/run.py`).
  The broker keeps the names of runtime libraries and the class, nothing the script
  read or printed. A script could send its own report, so the counts are a measure,
  not a record anything depends on.
- **Each read of the notes.** The broker records each `read_api("python...")`, by
  principal.
- **`make library-use`** counts the runs that reported what they used, and leaves out
  those that sent no report: runs from before reports began, and runs stopped at a
  time or memory limit. It prints how many it left out. For each library over the
  last 30 days (`DAYS=`), it prints the runs that imported it, those of them that
  failed, the failures raised inside it, the runs that called `help()` on it and how
  many calls, and the reads of its notes. After the table, it names the exception
  classes the failed runs ended with, for each library. A class the script defined
  itself shows as `other`.

How to read it, once at least 10 runs in the period have imported a library:

- **Failures with AttributeError or TypeError, inside the library or on its
  objects, in more than 1 run in 5:** agents reached for an interface that isn't
  there. Add the entry points they need to the library's notes. Other failures, such
  as a parser error, are usually about the data.
- **`help()` in more than 1 run in 3:** agents look up the same thing each time. Put
  it in the notes.
- **A library nobody imports** while scripts do the work it is for, such as grouping
  rows by hand: check that `search_api` finds it, and that its summary names the task.

The counts show where the notes fall short, but not whether better notes would make
the scripts cheaper. That needs the same measured task run with and without the
notes, which is planned ([planned/agent-guidance.md](planned/agent-guidance.md)).

## Where the guidance was first tested

The first measurements ran every task in a fresh session, the case with no experience
to draw on. Script mode appended a short system prompt that described the helpers and
when to use them. That prompt was the first version of this guidance.
[../research/measurements.md](../research/measurements.md) records what the runs
cost.

## Planned

[planned/agent-guidance.md](planned/agent-guidance.md) designs what is not built:

- **Guidance at the moment of a call:** Claude Code hooks, a Claude Code skill, and a
  plugin that bundles them with the gateway's configuration.
- **Recognition from repetition:** a nudge at the third call to one tool, and tool
  notes that carry what earlier sessions learned.
- **Getting better from the run log:** proposals for skills, and the estimate's
  constants checked for each model.
