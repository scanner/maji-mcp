# Code Mode, applied to how you work

**Status:** a record, written before maji-mcp was designed (early November 2026) and
kept as written. The notes explain the idea maji-mcp builds on. The design that
followed is in [../design/architecture.md](../design/architecture.md).

## The idea in one paragraph
Today the model calls tools one at a time, and every result passes back through its context before
it can choose the next call. Code Mode turns the MCP servers' tools into a typed API in a
programming language. The agent gets **one** tool, "run this code," and writes a script that calls
that API. The script loops, filters and joins inside a sandbox and prints only the final
answer. The sandbox's only outside access is through stubs, which forward calls to a host process
that holds the credentials, so the code never sees a key.

The case for it is reasoned, not measured. Models have seen far more real code than tool-call
transcripts, and the intermediate data stays out of context. Cloudflare gives no
benchmarks. Anthropic's later post on the same idea gives one figure: a workflow fell from about
150k tokens to about 2k. Both posts use TypeScript, but nothing in the idea depends on that
language.

## 1. Your own commit history already supports the thesis
The clearest case is `eb49d7f` (#20). An agent kept using awk and grep on `tasks.org` and got three
answers confidently wrong. Your diagnosis was: *"The server already had a correct parser. The agent
kept reimplementing a worse one because the correct one could not be asked the question."*

That is Code Mode's argument, seen from the other side. **The agent wanted to write code. The only
code it could reach was shell against raw text.** Your fix was to grow the tool parameters with
`where`, projection, `detail="items"`, paging and `org_stats`. That is a small query language
encoded in JSON arguments. It works, but each new question needs a new parameter, plus a
description, plus a test.

The Code Mode answer is to let the agent write Python against the parser itself:

```python
tasks = list_tasks("Active Tasks", where={"PROJECT": "billing-sync"})
print(sum(1 for t in tasks if "[ ]" in t.body), "open checklist items")
```

None of those 350 lines of checklist passes through context, and nobody has to add a parameter.

## 2. How it changes the way you build MCP servers
Don't read this as "the revamp was wasted." Split the server in two:

- **The read side is where Code Mode takes away most of the surface.** Filters, projection, counts,
  paging and cross-scope unions (#14, #20, #21) are all ways of asking the model to compose a query
  out of JSON. Code composes queries natively. You would keep the primitives (`list_tasks`,
  `find_task`, `search`) and drop most of the flags.
- **The write side stays as it is, and it matters more.** Several fixes encode rules you wrote down
  as *"Guidance cannot be enforced; code can"*:
  - #17: linking maintains both ends of the task–project link.
  - #23: `mint_custom_id` reads the archives itself.
  - #15: a task's position means its priority.
  - #13: re-rendering a heading keeps its progress cookies.

  In Code Mode these become the library functions the agent is *allowed* to call. If the agent can
  only write by calling `link_task_to_project`, it cannot skip the step that writes the other
  end. Hold this line: **the sandbox gets your invariant-keeping functions, never raw file
  access.** Otherwise you have rebuilt the awk problem with extra steps.
- **Writes need a checkpoint the sandboxed code can't skip.** A script that calls
  `create_journal_entry` doesn't write anything itself: the host records the proposed change,
  and applies it only after you approve it (see [control-plane.md](../design/control-plane.md)). In Cloudflare's design,
  the bindings are where checks like this live.
- **Your docstring rules become the API contract.** In #14 you fixed tool descriptions that
  misstated what each call returns. In Code Mode the model reads Python signatures and Google-style
  docstrings instead. Your CLAUDE.md rules carry over directly: say what it returns, anchor to real
  names, use single backticks. Type hints stop being only style and become the interface the model
  reads.
- **Context cost is a real argument for you.** emacs-org-mcp has 26 tools plus about 18KB of format
  guides. This session already shows a partial fix: most tools load on demand through
  ToolSearch. Anthropic's post goes further and puts each tool in its own file, which the agent
  reads only when it needs it.

## 3. How it changes the way you use Claude
- **There is a cheap experiment you can run today, with no sandbox.** Claude Code already has Bash
  and Python, so it already does a rough version of Code Mode. In #20 the problem was that the code
  it wrote couldn't reach the correct parser. Your parser is nearly importable already:
  `tasks.list_tasks(section_name, where)` returns `Task` objects. The one catch is that it reads
  `global_state.config`, so you need a short shim that calls `load_config` and fills it in. Wrap
  that as a `uv run` entry point, mention it in the project's CLAUDE.md, and watch whether Claude
  prefers it to `list_tasks` calls for survey-style questions. That tests the thesis on your real
  data before you build any infrastructure.
- **Long-running jobs can save their state.** A script can write intermediate results to a file and
  pick up from it later, instead of carrying them in conversation context.
- **Working scripts become skills.** When Claude writes a useful query script, save it as a named
  function with a `SKILL.md`. Anthropic's post suggests exactly this. Over time your "API" becomes
  a mix of hand-written primitives and scripts that proved themselves.

## 4. The catch with Python
Cloudflare's speed and security story rests on V8 isolates and Workers bindings: a fresh sandbox in
milliseconds, with no network except the bindings. **Python has no direct equivalent.** The options
include:
- a subprocess with a restricted environment
- Pyodide (Python compiled to WebAssembly) under Deno or another WebAssembly runtime
- containers
- restricted interpreters

Each trades away some of startup speed, completeness of the Python language, or isolation
strength. The property you need, whichever you pick, is this: **the code can reach the outside only
through stubs that send each call back to a host process. The host holds the credentials and
enforces approval.** That is the hard design problem. Generating the Python API from MCP schemas is
the easy part, and you already write servers in Python.

If you only call the Claude API directly, Anthropic's **programmatic tool calling** handles this
for you. Claude's code runs in Anthropic's container and pauses on each call to one of your tools,
which you execute and send back. That doesn't help Claude Code with your local MCP servers, though,
which is where you spend your time.

## Where `maji-mcp` fits
The empty `maji-mcp` repo reads like the seed of exactly this: one MCP server that connects
to your other servers as a client, generates typed Python stubs from their schemas, and exposes a
single `run_python` tool. If that is the plan, decide on a sandbox and on how approval crosses the
sandbox boundary before writing any code. The rest is routine.

**Sources:**
- [Cloudflare: Code Mode](https://blog.cloudflare.com/code-mode)
- [Anthropic: Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp)
- [Anthropic docs: Programmatic tool calling](https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling)
