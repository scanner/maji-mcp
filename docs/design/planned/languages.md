# Planned: skills in other languages

Python runs the system, and skills can be written in other languages.

**Status:** planned. Python is the only runtime built. Two early decisions from this
document are in place: the broker's protocol is language-neutral JSON-RPC, and every
skill has a manifest ([../skills.md](../skills.md)). The manifest does not yet carry a
`runtime` or input and output schemas, and the runner takes a runtime and a script,
not yet an artifact and arguments.

The gateway, broker, control plane and runner are written in Python. A skill is a
separate process, so it can be written in any language the executor can build and run:
Go, Rust, C, Java, JavaScript, Common Lisp. Each language added means toolchains in
containers, and that is where the design changes.

## Building runs untrusted code

A compiler doesn't only read source code. Builds also execute code from the source and
its dependencies:
- Rust build scripts and procedural macros
- cgo
- Java annotation processors
- npm install hooks

Building an agent-written skill is therefore running agent-written code, so builds
follow the same rules as runs:

- **Builds run in the sandbox, with no network.** A build that can reach the network is
  a way to exfiltrate data or fetch a dependency nobody reviewed.
- **Dependencies come from a pinned allowlist per ecosystem,** pre-fetched into the
  build image or served from a local mirror that holds only allowed versions.
- **Adding a dependency is a queue item.** The control plane shows the package, the
  version, and what it pulls in with it.

## Build and run are separate stages

Each runtime gets its own pair of images, all sharing one sandbox contract:
- CPU, memory, process and time limits
- an output cap
- a read-only filesystem except the workspace
- no network except the broker's socket

| Runtime | Build image | Run image |
|---|---|---|
| Python | none; dependencies are pre-installed | the interpreter and the allowed packages |
| Go, Rust, C | the toolchain plus vendored dependencies | the static binary on a minimal base |
| Java | the JDK plus vendored dependencies | a JRE |
| JavaScript | none, or a bundling step | a runtime such as Deno, which denies file and network access unless granted |
| Common Lisp | SBCL plus vendored systems | an SBCL core image |

Separate images per runtime keep each run image small, and keep toolchains out of the
run stage entirely. One image holding every toolchain would make each run carry several
gigabytes, and the attack surface of every compiler in it.

## Source is the artifact of record

A skill is its source code. A compiled binary is always derived: each instance builds it
from the source, in that instance's own build image. So:

- **The promotion signature covers the source, the manifest and the lockfile,** plus,
  for a runtime that builds, the digest of the build image it was tested with. It never
  covers a binary.
- **Packs carry source only.** An export contains the code you and the agent can read,
  never a binary. An import that contains a binary is rejected.
- **Generated stubs are not shipped either.** They are regenerated on each instance from
  its own schemas.
- **Upgrading a toolchain changes the build image digest,** so every skill in that
  language is rebuilt and re-run against its tests before it runs again.

**A persona's instances can share a build cache.** Building Rust or Java on every
machine is slow, so the cache key is a hash of the source, the lockfile and the build
image digest. The catch is that a cached binary can't be checked against its source
without building it again. So the cache needs a trust rule of its own:
- **Each instance's runner signs what it builds,** with a key held by the runner's OS
  user. These **runner keys** are a separate set from the persona's trust set. They are
  accepted for build-cache entries and nothing else. Promotions, revocations, import
  pins and decisions always need one of your own keys.
- **An instance accepts a cache entry only if one of the persona's runner keys signed
  it.** One compromised machine could otherwise put its binaries on every other one.
- **Where the toolchain builds reproducibly, an instance can rebuild a sample of
  entries in the background** and check the hashes match. How reproducible a build is
  varies by toolchain and build flags.
- **With no trusted entry, the instance builds locally.** The cache only ever makes
  things faster, never changes what runs.

## Dependencies are the exception

Skill code is reviewed source. Third-party dependencies, the packages a skill gets
through `uv add` or `pip install`, are trusted a different way: by **pin**, not by
review.
- The skill's lockfile records each dependency's exact version and hash. That is what
  `uv.lock` already does.
- Each instance installs only versions on its own allowlist, from its own mirror.
- A dependency the receiving instance hasn't allowed becomes a queue item on import.

The pin doesn't mean anyone read the dependency, and it can be a prebuilt binary, such
as a compiled Python wheel. That is accepted on purpose. A published package has its
own provenance and a public history, while agent-written code has neither, so only the
skill's own code has to be source you can review.

## You can only vouch for code you can read

A promotion signature means you reviewed the code and vouch for it. That works well for
Python, which you know well. A Rust, C or Java skill you can't read closely makes the
same signature weaker, because you would be signing code you can't fully check.

So promotion policy is set per language:
- **Languages you read well:** promoted as usual.
- **Languages you read less well:** before you sign, the advisor agent explains the
  code, and the skill's tests have to pass. Both appear on the queue item. The advisor
  still decides nothing.
- **Languages you don't read at all:** usable for drafts and candidates on their own
  machine, but not promotable until you change the policy.

## Lisp

- **Common Lisp (SBCL)** is an ordinary runtime. It runs in a container like any other
  language.
- **Emacs Lisp** has two runtimes, covered in [emacs-lisp.md](emacs-lisp.md):
  - batch Emacs, which runs in the executor and is sandboxed like any other runtime
  - your live Emacs, which can't be sandboxed and is reached through the Emacs MCP
    server ([emacs-mcp.md](emacs-mcp.md))

## Skills calling skills: the runner

A skill in one language calling a skill in another can't import it, so cross-language
calls go through a **runner**. The runner is the component that launches sandboxed
builds and runs. The gateway asks it to run agent code, and the broker asks it to run a
skill on another skill's behalf.

This makes skills the same kind of thing as stubs:
- **Every skill declares input and output schemas in its manifest.** The runner
  validates both.
- **A promoted skill is callable from every language,** and the gateway can offer it to
  agents as a tool. This is how the layer grows new tools out of the code agents write.
- **The original caller's identity and resource budget pass down the whole call
  tree.** A skill called on an agent's behalf runs with that agent's permissions and
  draws on that run's limits, with a limit on call depth.

Same-language calls can stay in-process, as imports, but an import goes through the
same loader check as everything else: signature, revocation record and run policy.
Without that, a promoted skill could import and run one that was revoked or never
promoted.

## Stubs in each language

The broker's protocol is language-neutral: JSON-RPC over the Unix socket. Each new
language starts with a small **dynamic client**: open the socket, send a method name
and JSON arguments, read JSON back. That is enough to make the language usable.

**Typed stubs** come later, generated from the same schemas the broker already has: MCP
output schemas and REST API specifications. They are added for a language where a
generator exists for it. Types matter more for agent-written code than for code a
person writes, because a typed stub turns a wrong field name into an error rather than
an empty result.

`read_api` renders the documentation for the agent's chosen language from the same
schema, so a Go script sees Go signatures.

## When to add a language

Add one when a specific skill needs it, for one of these reasons:
- **Speed:** parsing or crunching large data. Go or Rust.
- **An SDK** that exists only in one ecosystem. Java or JavaScript.
- **A library** that has no Python equivalent.

How fast a runtime starts decides where it fits:
- Fast-starting runtimes (Python, JavaScript, Go) suit drafts, where the agent iterates.
- Slow-compiling ones (Rust, Java, C++) suit promoted skills, where the build cost is
  paid once and the artifact is cached.

On import, a pack's `runtime` is checked against the runtimes the instance has, the
same way `requires.stubs` is checked against its stub catalog.

## Decisions to make now

These are cheap now and expensive to retrofit:
- The broker protocol is JSON-RPC, not Python-specific. *Built.*
- Promotions and packs carry source only. Binaries are always built per instance.
- Every skill manifest carries `runtime`, plus input and output schemas. *The
  manifest exists; these fields are not yet in it.*
- The executor is reached through a runner interface that takes a runtime, an
  artifact and arguments. Python is just the first runtime behind it. *The runner
  takes a runtime and a script today.*
- In-process imports go through the loader's trust check. *Built for skills that
  call skills: each skill is loaded through the broker's checks.*

## Building it

In order:

1. Build and run image pairs, and the build container's settings.
2. The build cache, keyed by source, lockfile and image digest, with entries signed
   by runner keys.
3. A dependency mirror and an allowlist for each ecosystem. Adding a dependency is a
   queue item.
4. A JSON-RPC client library for the second language, chosen by the first skill that
   needs one.
5. Typed stub generators, where one exists for the language.
6. Calls between skills through the runner, passing the principal and the resource
   budget down the call tree, with a depth limit.
7. Promotion policy for each language.

**Done when:** one skill in a second language runs through the broker.
