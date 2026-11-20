# Planned: Emacs Lisp skills

Skills that work in the editor.

**Status:** planned. Nothing in this document is built. The document depends on other
runtimes ([languages.md](languages.md)) and on the Emacs MCP server
([emacs-mcp.md](emacs-mcp.md)), a separate project that does not exist yet.

Your Emacs already holds a lot of state an agent can use: buffers, projects, diagnostics,
git state, org data. Emacs Lisp skills let agents use that state, and change it, through
code that is kept, tested and promoted like any other skill. There are two runtimes,
because Emacs Lisp runs in two very different places.

## What is already there

claude-code-ide.el connects this Claude session to Emacs with two MCP servers:
- **The IDE server:** `openFile`, `getDiagnostics`, `openDiff`, and `executeCode`.
- **The emacs-tools server:** xref, imenu, tree-sitter and project info. It also offers
  `claude-code-ide-make-tool`, which exposes any elisp function as an MCP tool with
  typed arguments.

`executeCode` evaluates any elisp form the agent sends in your running Emacs, with
`(eval (car (read-from-string code)) t)`. The tool itself asks for no confirmation.
Whether Claude Code asks first depends on your permission settings. It is on by
default (`claude-code-ide-enable-execute-code` defaults to `t`), and your configuration
leaves it on. The agent can also reach the same Emacs by running `emacsclient --eval`
in Bash. This is the same issue as point 3 in
[../../research/evaluation.md](../../research/evaluation.md), the agent's own shell,
seen through a second channel.

maji-mcp doesn't replace these paths or restrict them. It reaches the same Emacs with
at least the same access, and adds what `executeCode` lacks:
- **Kept code.** Elisp that is tested and promoted, instead of one-off forms.
- **Structured results.** `executeCode` returns `(format "%S" result)`, which is text
  again. maji-mcp skills return JSON.
- **Composition.** One script can combine editor state with Sentry, Jira or org data.
- **A run log** of what ran in your editor, and on whose behalf.
- **Reach.** Agents that have no shell, or that aren't running inside Emacs, can still
  use the skills.

## Two runtimes

**Batch Emacs Lisp** (runtime `elisp-batch`) runs `emacs --batch -Q` in the executor,
sandboxed like any other runtime. It has no access to your session, but it has the
whole of Emacs as a library:
- **The real org parser.** `org-element` is the canonical org parser. emacs-org's
  bugs #12 and #13 came from orgmunge, which isn't.
- **Text processing.**
- **Org export.**

Files are mounted read-only, and results come back through `json-serialize`. Elisp
packages a skill needs are pinned in its lockfile and installed in the build image, like
any other language's dependencies.

**Live Emacs Lisp** runs inside your running Emacs. It cannot be sandboxed. It runs with
your full user power, and it can reach more than a fresh shell can:
- decrypted auth-source and gpg-agent state
- open buffers
- live TRAMP connections to remote hosts

It is also local to one machine. A skill whose runtime is `elisp-live` runs only on an
instance that has an Emacs attached. The import and run checks treat that like any other
runtime an instance may not have.

## Access: at least what Claude Code has

maji-mcp offers two ways into the live Emacs:
- **`emacs.eval`** evaluates an ad-hoc elisp form, as `executeCode` does. It returns
  JSON, records the form in the run log, and can be called from a script, so one Python
  draft can mix editor state with other sources.
- **Promoted live skills,** called by name.

Who gets which is set per principal at the broker, like every other stub:
- **The local session you're driving gets `emacs.eval`.** That matches what Claude Code
  has today, so nothing is lost by going through maji-mcp.
- **Other principals get promoted live skills only,** unless you grant more. These are
  remote and cloud agents, and agents on other machines. Giving them raw eval would be
  more access than Claude Code has today, not the same.

## How the broker reaches your Emacs

- **Through the Emacs MCP server.** This is an MCP server running inside your Emacs, as
  you. It is a separate project, described in [emacs-mcp.md](emacs-mcp.md), and it
  implements the server contract ([../server-contract.md](../server-contract.md),
  [server-contract.md](server-contract.md)), including its runtime host profile. To
  the broker it is one more MCP server, behind the same RBAC, rate limits and run log
  as the rest.
- **It hosts promoted skills.** The broker checks a skill's signature and the revocation
  records, then loads it with `load_skill`, and calls it by namespaced name with
  `call_skill`. Results come back as JSON.
- **It offers `eval`** for principals granted `emacs.eval`.
- **The emacs-tools HTTP server is the wrong channel for the broker.** It starts with
  the first Claude session, stops with the last, and is addressed per session.
- **Promoted live skills can also be published to claude-code-ide.** Registering a skill
  with `claude-code-ide-make-tool` makes it a native tool in every Claude session hosted
  in Emacs. That path calls the skill directly and skips the broker's run log, so
  publishing this way is a choice you make per skill.

## Rules for live skills

- **Return JSON, not printed Lisp.**
- **Keep them small and bounded.** Emacs runs Lisp on one thread, so a slow or looping
  skill freezes your editor. In a test, `with-timeout` did not interrupt a busy loop,
  so a time limit can't be enforced from inside Emacs. `C-g` is the only stop. Heavy
  work belongs in batch elisp or Python. Live skills gather editor state or apply
  changes to it.
- **Promoted skills use buffers as the changeset.** A promoted skill that changes files
  edits their buffers and leaves them unsaved. You review the change with
  `diff-buffer-with-file` and save it. This works only on buffers with no unsaved edits
  of yours, so the two never mix. For any other buffer, the skill's change becomes a
  changeset in the queue. Ad-hoc eval isn't held to this convention, just as
  `executeCode` isn't.
- **Ad-hoc eval runs directly,** as `executeCode` does today, with no queue in between.
  Promotion is what a live skill needs before other principals can run it, and before
  it can be published to claude-code-ide.
- **Promotion carries the most weight here.** You read elisp well: you wrote emacs-org's
  archive and ediff bridges. So elisp gets normal promotion. But a live-elisp signature
  vouches for code that runs with everything your Emacs can reach.

## What it makes possible

Using what is installed today (Emacs 29.4, org 9.6, magit, flycheck and flymake,
projectile, tree-sitter):
- **What am I working on:** the current buffer, point, region, project and modified
  buffers, as data.
- **Diagnostics across every open buffer in a project,** grouped by file.
- **Magit status as data,** with staging a hunk as an applied change.
- **The org agenda for a date, and clock totals,** computed by org-mode itself.
- **Task-item edits through org itself:** tick, update, add or delete one checklist item,
  or add a link, with org keeping the progress cookie right. Today an agent regenerates
  the whole task to do this: about 800 tokens for a median active task, and up to about
  14,000 for the largest.
- **Showing you results:** open a report buffer, jump to `file:line`, highlight a
  region, or open a diff.
- **Combining sources:** a Python script finds the top Sentry frames for a release,
  then calls a live skill that opens each file at its line in one report buffer.

No language server is configured today. Eglot is built into Emacs 29. With it enabled,
skills could get references, types and renames from a server already running in your
Emacs, instead of the agent searching with grep.
