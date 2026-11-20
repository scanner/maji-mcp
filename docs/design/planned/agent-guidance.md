# Planned: guidance that reaches agents sooner

How an agent will learn, at the moment of a call, that code or a skill is the better
tool, and how the guidance will improve from what the run log shows.

**Status:** planned. Nothing in this document is built. What is built is in
[../agent-guidance.md](../agent-guidance.md): the signals, the cost estimate, the
gateway's instructions that carry both, and the run log's counts of how scripts use
the runtime libraries.

The built channels depend on the model noticing a signal by itself. An agent usually
has to use an MCP server several times before the agent sees that the work would be
simpler in code: the third Sentry lookup, the second large task rewrite. Each new
session starts without that experience. The work below puts the guidance where the
agent makes the choice, including when the agent is using another server directly and
has not consulted maji-mcp at all.

## More ways the guidance will reach an agent

1. **Tool descriptions worded in terms of the signals.** A change to a description
   changes what every agent reads, so each change will be measured against the runs
   before it.
2. **A Claude Code skill.** The skill will be triggered when the agent is about to do
   one of the signalled things against any MCP server, maji-mcp or not. The skill
   will carry the full signals table, worked examples, and how to search the skill
   library.
3. **Claude Code hooks, as nudges at the moment of a call.**
   - A `PreToolUse` hook on known expensive tools, such as emacs-org's `update_task`,
     will compare the new body with the current body. When the change is small
     against the size sent, the hook will point to the matching skill.
   - A `PostToolUse` hook on large results will suggest filtering in code next time.
   - The hooks will count calls for each tool and each record within the session.

   A hook sees the call before or after the call runs, so a hook is deterministic
   where the other channels depend on the model noticing. The exact fields a hook can
   return are to be checked against Claude Code's hook documentation when building.
4. **A Claude Code plugin** that bundles the gateway's MCP configuration, the skill
   and the hooks, so one install sets all of them up. Agents in other apps will get
   the built channels.

## Recognition from repetition

**Within a session, the hooks will point out the repetition early.** The hooks count
calls for each tool and each record. At a threshold, a hook will say what the agent
is doing and what the alternative is. At the third call to one tool with different
arguments: "this is the third `get_issue_details` call; a loop in `run_python` can
fetch the rest." At the second large write to the same task: "the last two updates
changed one line each; `complete_task_item` does this in one call."

**Across sessions, maji-mcp will keep what earlier sessions learned.** Once the run
log shows a pattern, the pattern will be kept in one of two forms:

- **A skill,** whose summary names the tool and the pattern the skill replaces.
- **A tool note:** a short line attached to one server's tool, such as "To tick one
  checklist item, use `complete_task_item` instead of `update_task`." A hook will
  show the note the first time the tool is used in a session, so the next agent has
  the note on its first call.

Every agent reads a tool note, so a tool note is open to injection the same way a
tool description is. A new or changed tool note will be a queue item, and will take
effect once you accept the item ([queue.md](queue.md)).

## Getting better from the run log

The run log records every call made through maji-mcp
([../broker.md](../broker.md)), and the hooks will report what they saw on direct
calls. Together they will show where guidance is missing:

- **Missed signals.** Writes that sent far more than they changed, and loops of direct
  calls a script would have replaced. Each recurring pattern will be proposed as a
  skill, as a queue item.
- **Recurring write shapes.** A write the agent cannot justify coding the first time
  can still be worth a skill across a week. The proposal will give the count and the
  cost. In one person's org history over 10 weeks, `update_task` was called about 40
  times a week, each call sending the whole task. Items were checked off about 30
  times a week, in tasks with a median of 7,600 characters.
- **Skills nobody finds.** A skill that exists and is not used in the situations the
  skill covers needs a better summary.
- **Guidance that misfires.** Scripts written where one direct call would have done
  will show up as runs with more output tokens than the calls the runs replaced.
- **The estimate's constants.** The run log will record each skill's measured saving
  per use and first-time cost, so the constants are checked for each model and
  corrected.
- **What the runtime libraries' notes are worth.** A measured task that sifts data or
  code, such as the Hotspots example in the roadmap, run with the notes and with only
  the names, will compare the scripts' failures, `help()` calls and output tokens.
  The comparison takes paid sessions, so it runs with your go-ahead.

Changes to the signals, the skill and the hooks will be versioned with the rest of
maji-mcp, and the measurements will be rerun after each change.

## Building it

In order, as [../../roadmap.md](../../roadmap.md) lists the work:

1. The run log records each skill's measured saving and first-time cost.
2. Tool descriptions worded in terms of the signals, measured against the runs before
   the change.
3. Claude Code hooks: a nudge before an expensive call, after a large result, and at
   a count of repeated calls.
4. Tool notes, each accepted through the queue, and proposals for skills from writes
   that sent far more than they changed.
5. The Claude Code skill, then the plugin that bundles the gateway's configuration,
   the skill and the hooks.
