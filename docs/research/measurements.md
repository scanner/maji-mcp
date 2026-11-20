# Measurements

**Status:** a record of two rounds of measurement, made in November 2026. The first
round was made before anything was built, and decided whether to build. The second
was made once `run_python` ran. This document keeps the method and the results. The
harness, the prompts, the per-run numbers and the sessions held one person's work
data, and are not kept in this repository. The next round of measurement will bring
a harness of its own.

The first round asked one question: on real tasks, does an agent that writes code
against structured data spend fewer tokens than an agent that makes a series of MCP
tool calls, and does it still get the right answer?

**Result: yes, for aggregation, joins and multi-source procedures. Every one of the 24
answers was correct in both modes. Writing code cost 68% less overall. The one small
write cost 26% more the first time, when the agent had to work out the code, and about
80% less once that code existed as a skill.**

**Decision: build** (2026-11-07), with skills given priority over one-off scripts (see
"Once the code exists" and "How much of a record an update changes").

**The second round** ran two of the tasks through maji-mcp's `run_python`. One met
its target. The other, once scripts could keep results and make calls at once, cost
5% less than the first round's script mode and read 18% less, but still wrote 17%
more (see "The second round"). The gap in output is left to revisit.

## Setup

- **Six real tasks,** each with a correct answer computed independently beforehand
  or, for the daily summary, graded by reading against a rubric.
- **Two modes, two runs each, every run a fresh headless Claude Code session.**
  - **Baseline:** the agent's usual MCP servers.
  - **Script mode:** the same servers, plus a short appended prompt describing helpers
    that return structured data, and one that calls an MCP server's tools from code.
- **Both modes had the same tool allowlist,** read-only except for one write tool in
  the write task, and files writable only in a scratch directory.
- **One model:** Claude Opus 5.5.
- **Tokens are totalled from every model call in the session,** subagents included.

## Results

Means of two runs per cell.

| Task | Kind | Cost, baseline → script | Output tokens | Tool calls | Correct |
|---|---|---|---|---|---|
| Control: one ticket's status | single lookup | $0.14 → $0.15 (+7%) | 506 → 640 | 3 → 3 | 4/4 |
| Task checklists: one project's open items and closed tasks | aggregation | $0.39 → $0.24 (−38%) | 3,549 → 3,607 | 11 → 6 | 4/4 |
| Error-tracker culprits: three filtered issue sets, deduplicated and grouped | aggregation | $1.71 → $0.82 (−52%) | 23,446 → 14,602 | 16 → 11 | 4/4 |
| Tracker × git × error-tracker join | join across three sources | $7.34 → $1.29 (−82%) | 108,400 → 20,328 | 74 → 23 | 4/4 |
| Daily summary from commits, reviews and tasks | multi-source procedure | $1.47 → $0.57 (−61%) | 26,048 → 10,035 | 32 → 16 | 4/4 |
| Write: tick one checklist item in a 10,000-character task | small write | $0.50 → $0.63 (+26%) | 7,381 → 6,237 | 6 → 10 | 4/4 |
| **All tasks** | | **$23.12 → $7.39 (−68%)** | | | **24/24** |

The cost column weights cache reads, cache writes and uncached input as the API bills
them.

## What the results show

**The join is where code wins most.** One baseline run spawned three subagents and made
110 tool calls, including one detail lookup per error-tracker issue, 51 in all. It cost
$11.81. The other baseline run made 37 calls for $2.88. Both script runs fetched the
issues once, filtered and matched them in a script, and cost $1.16 and $1.42. All four
answers were identical and correct. Even against the cheaper baseline run, script mode
cost about half as much.

**Aggregation and multi-source procedures roughly halve the cost.** The error-tracker
task and the daily summary both read many records to keep a few. In script mode, the
records stayed in the script.

**A single lookup costs the same either way,** as the control task was meant to show.

**Every answer was right in both modes.** So the gain is cost and time at equal
quality, not a trade of accuracy for tokens.

## What the write task shows

Ticking one checklist item through an MCP server's existing tools means fetching the
task and sending the whole task back. Both modes did that correctly: the item, its
progress count and the task's modified stamp changed, and nothing else did.

**The first time in code cost 26% more.** The agent had to work out how to do it: it
read the helper's source, in one run the server's source too, and checked its work. It
wrote fewer output tokens than regenerating did, 6,237 against 7,381. The extra cost is
all in what it read.

## Once the code exists

A follow-up measured the write the other way round: once the code has been written
once, what does each tick after that cost?

`complete_task_item` is the code a script-mode run wrote, made general, and is kept in
`tests/local/org_skills.py`. It finds the
item by its text, checks it, recomputes the section's progress count, and saves the
task. It refuses, changing nothing, when the text matches no item or several, or the
item is nested. Skill mode describes it in one paragraph of the session's prompt. It
ran on the same task, and on a larger one near the 90th percentile of the tasks where
items get checked off.

| Task | Regenerate the task | With the skill | Correct |
|---|---|---|---|
| 10,000 characters | $0.50, 7,381 output tokens, 6 calls | $0.10, 1,475 output tokens, 4 calls | 4/4 |
| 14,600 characters | $0.59, 9,630 output tokens, 12 calls | $0.13, 1,168 output tokens, 4 calls | 4/4 |

- **Regenerating grows with the task, and the skill doesn't.** The larger task's
  baseline runs also spent calls checking that 14,600 regenerated characters matched
  the original exactly. That check is part of the cost of sending the whole task back.
- **Output tokens are the better guide to a tick in the middle of a session.** The
  dollar figures are whole fresh sessions, and include $0.06 to $0.22 of start-up,
  depending on how warm the cache was. A single lookup, the control task, produced
  about 500 output tokens. Over that floor, a tick added about 700 to 1,000 tokens
  with the skill, and 6,900 to 9,100 by regenerating.
- **The code pays for itself on its first reuse.** Measured in price, with each run's
  start-up cache write taken out, the first time in code cost $0.08 more than
  regenerating, and each tick after it saved $0.35. The two baseline runs differed
  threefold, so at worst the code pays back by its third reuse.

**How often it happens.** From the git history of one person's task files, over 9.6
weeks:
- The update tool was called about 40 times a week, each time sending the whole task.
- Items were checked off about 30 times a week, in tasks with a median of 7,600
  characters, and 16,800 at the 90th percentile.
- **Only 2 of the 120 commits that checked off items did nothing else.** The rest also
  changed a median of 39 other lines: a note on the item, new items, a paragraph on
  progress.

So the tick on its own is rare. It usually comes as part of a larger update.

## How much of a record an update changes

The update tool takes the whole task, so every update sends every section, however
little changed. Each task was compared before and after each of its 385 updates,
section by section.

| Updates that changed | Share |
|---|---|
| 1 section | 11% |
| 2 sections | 23% |
| 3 sections | 28% |
| 4 or more | 38% |
| every section | 4% |

The median task has 8 sections, and the median update changed 3 of them. What each
kind of edit would have had to send, over all 385 updates:

| Edit | Characters sent | Share of whole |
|---|---|---|
| Whole task, as the update tool takes it | 4,539,892 | 100% |
| Changed sections only | 1,945,973 | 43% |
| Added lines only, as a patch would | 652,175 | 14% |

**What this shows.** Updates are focused on a few sections. Few change only one
section, and almost none rewrite the whole task. An edit that replaces only the changed
sections would send less than half as much, and a patch about a seventh. Output tokens
cost several times what input tokens do, and an agent writes them one at a time, so
this is time saved as well as money. A narrower edit also limits what can go wrong to
the sections named: a whole-task rewrite can damage a section the agent never meant to
touch.

That makes the edit to build a section-level one, with checklist operations: replace
or append to named sections, add sections, and check off or add items
([../design/planned/writes.md](../design/planned/writes.md)).

## Findings along the way

- **One page of 100 tracker issues through its MCP server cost about 10,000 tokens,**
  almost all of it per-issue boilerplate. A query narrowed to the keys that mattered
  took one short call.
- **An older endpoint of the error tracker gives quietly wrong answers.** It uses only
  the last environment filter given, and reports lifetime counts. A newer endpoint
  treats several environments as any-of and counts within the time window. Every
  agent run happened to use the newer one, but a confident wrong answer was one
  endpoint choice away.
- **Text takes more tokens than the usual rule of four characters per token.**
  Measured with Claude Opus 5.5, org text came to 2.3 characters per token, and Python
  to 2.7. A 10,000-character task is about 4,400 tokens.
- **A session's top-level `usage` undercounts.** It leaves out subagents, and is
  incomplete even without them. `modelUsage` matches the billed cost.

## Caveats

- **Two runs per cell.** The join's baseline varied fourfold between its two runs.
- **Fresh sessions only:** the case where an agent has no experience of the task.
  Repeated use within one session would likely narrow the gap for the baseline, and a
  skill library would widen it for script mode.
- **The allowlist handicapped script mode slightly.** Shell idioms such as `for` loops,
  redirects to files, and changing directory are refused when no one is there to
  approve them, and the agent spent a few turns working around that.
- **The write task ran against a copy of the files.** An early fault in the harness had
  the read helper reading the real files while the write went to the copy, which
  inflated the first script-mode runs. The fault was fixed and those runs were made
  again. Runs were set aside only as whole cells, never for their result alone.
- **The cache's warmth varies between runs.** A run that follows another starts with
  the system prompt already cached, and costs up to $0.16 less. Output tokens don't
  depend on the cache, which is why the follow-up compares them.
- **One model, one machine, one person's tasks.** The tasks were chosen as real work, so
  they are representative of that work, not of agents in general.

## The second round

The second round ran the two tasks that need only error-tracker and task reads
through maji-mcp itself. The agent's only tools were the gateway's: `run_python` and
the API browser. Its scripts ran in sandboxed executors, and their stub calls went
through the broker to Sentry, and through the bridge to emacs-org-mcp.

**Result: all eight answers were correct. Task checklists met the target: fewer input
and output tokens than the first round's script mode, at the same cost. Error-tracker
culprits first used 9% more input, 31% more output and 7% more cost than script mode.
Once scripts could keep results for each other and make calls at once, it used 18%
less input and cost 5% less, but still wrote 17% more output.**

Means; the first round's script mode → through `run_python`.

| Task | Runs | Cost | Input tokens, all kinds | Output tokens | Tool calls | Correct |
|---|---|---|---|---|---|---|
| Task checklists | 2 | $0.24 → $0.23 (−2%) | 206,908 → 186,673 (−10%) | 3,607 → 2,942 (−18%) | 6 → 4 | 2/2 |
| Error-tracker culprits | 4 | $0.82 → $0.88 (+7%) | 586,122 → 638,084 (+9%) | 14,602 → 19,124 (+31%) | 11 → 12 | 4/4 |
| Error-tracker culprits, with `maji.save` and `maji.parallel` | 2 | $0.82 → $0.77 (−5%) | 586,122 → 478,995 (−18%) | 14,602 → 17,013 (+17%) | 11 → 9 | 2/2 |

- **The same prompts, word for word,** with the correct answers computed again just
  before the runs, on a stack of its own with its own principal, policy and port.
- **The record stubs did the job of the first round's helper.** Each checklist run
  called `read_api` once and ran two scripts, and wrote less than script mode did.
- **The error-tracker runs all answered correctly, but wrote more.** Two were on a par
  with script mode. The other two kept checking after they had the answer, with 11
  and 12 scripts where the first two ran 8 each.
- **The time limit wasn't the cause.** Runs with 60 seconds for each script and runs
  with 120 cost the same. The default is 60 seconds, and a script can ask for up to
  600.
- **A query takes 1 to 4 seconds from the broker,** and a script's calls ran one after
  another. A script that checks a count environment by environment spent most of a
  minute waiting. `maji.parallel` came from this.
- **Each `run_python` script starts empty.** To check a figure, a script fetched its
  data again. `maji.save` and `maji.load` came from this
  ([../design/runner.md](../design/runner.md)).

After those two additions, with the prompt unchanged, the agents learned of both from
`run_python`'s description:

- **Both runs used both.** Each saved its fetched issues early, and every later script
  loaded them. Each ran its per-environment queries through `maji.parallel` once.
- **Less of everything but output.** 5 and 8 scripts, where the first four runs took
  8 to 12, and 8 and 11 turns, where they took 11 to 15. Input fell below script
  mode's, and the cost with it.
- **Output stayed above script mode's,** at 14,989 and 19,037 tokens against 12,767 and
  16,436. Most output is the agent's reasoning, not its code. The changes made each
  check cheaper; the agents still checked.
- **Why the cost moved so little.** Weighted by price, a run's output is about half its
  cost, uncached input about a quarter, cache writes 12 to 18%, and cache reads 10 to
  13%. Most of the input saved was cache reads, the cheapest tokens, while output, the
  dearest, rose. Script mode's own two runs differed by 27% in cost, far more than
  the 5% between the modes: on cost, maji-mcp matches script mode, and output is where
  any further saving would be.
- **Few runs, on two days.** The first round's script runs are from the day before,
  and two to four runs a cell are few.
