# Planned: more in the queue

What the queue will hold beyond mounts and promotions, and what will help you decide.

**Status:** planned. The control plane's data model and API already carry most of what
this document describes: the kinds `question` and `server_registration`, the six risk
reasons, an item's `words`, and the gateway's internal socket
([../control-plane.md](../control-plane.md)). The parts that produce them are not
built.

## Questions

The queue suits questions that need your answer, not only approvals. Several agents,
or several steps of one long job, can each need an answer from you at once:

- "Which of these three projects did you mean?"
- "Is `eu-west-2` production?"
- "Which repository holds this work?"

Asked as prompts, the questions interrupt you one at a time, and each blocks its own
session. As items, the questions wait in order with the context that produced them,
and each asker carries on with other work.

- **A question is an item of the kind `question`.** The title is the question itself,
  in the requester's words and labelled so. The item carries the choices, if any, and
  why the answer matters, as the claim.
- **A question from an agent** arrives through a gateway tool. The gateway submits
  the question on the control plane's gateway socket, which can set no facts and
  offer no pattern, so nothing an agent writes can widen access.
- **A question from an MCP server** arrives through MCP elicitation. The broker
  receives the request through the SDK's `elicitation_callback`, and raises an item
  in place of blocking.
- **An answer can become a policy rule.** When you give the same answer to the same
  question every time, the control plane proposes the answer as the default.

## Tools for an agent's own items

Three gateway tools: `submit`, `status` and `withdraw`.

- **`status` answers at once and never waits.** An agent learns of a decision by
  being told: the agent ends its turn naming the item, and you tell the agent to go
  on once you have decided.
- **A principal sees and withdraws only its own items.**

## The advisor

An advisor is a different agent from the requester, with its own endpoint and
credentials. The advisor gives advice and decides nothing.

- **The advisor sees only what you would see:** the item's facts and claim, never the
  requester's conversation. The advice is then independent of the reasoning that
  produced the request.
- **The advisor can summarize, group, flag and annotate items, and recommend a
  decision.** The advisor's note appears on the item, labelled as the advisor's,
  beside the facts.
- **The control plane enforces the separation.** The control plane refuses an
  annotation from a principal or session that matches the item's requester.
- **The advisor's note is evidence to weigh.** The advisor reads the same injectable
  content as anyone. Running the advisor on a different model from the requester
  makes the advisor less likely to fall for the same injection. A tool that analyses
  risk can serve as one of the advisor's tools
  ([Risk analysis by a security tool](#risk-analysis-by-a-security-tool)).

## Your words on an item

A `UserPromptSubmit` hook in the agent's app will pass what you typed to the control
plane, and an item will carry the words that led to it, behind a disclosure.

- **The words clarify a request and approve nothing.** The agent could forge them, so
  the words are never treated as proof. The decision is on the broker's facts.
- **To work out:** tying a prompt to the item the prompt led to. The hook knows the
  app's own session id, and the gateway knows only the session the headers helper
  names.

## Telling you that items wait

With the queue's tab buried, a blocking item can wait unseen. The queue exists so you
can look at other things and come back.

- **One notification covers everything waiting.** The notification is sent only once
  an item has waited some minutes with no queue tab in view, at most once in a longer
  interval, with quiet hours.
- **Ways to send it:** Web Push from the web client's service worker, since
  `localhost` counts as a secure context, or a notification on the Mac through a
  bridge.

## Which agent is asking

- **A token for each agent is the part to trust.** An agent set up with its own
  gateway token is its own principal, and grants and policy are already per
  principal.
- **What the app says it is** is already shown on an item, labelled as unchecked.
- **The model** is not carried by MCP. A hook in the app can report the model, but
  the report is self-made, and one session can switch models or hand work to a
  subagent on another model.

## More findings about risk

The broker raises two of the six risk reasons today. The rest wait for the flows that
produce them:

- **`data_leaves`:** an export, a key sent to a host for the first time, or write
  scopes added to a connection.
- **`your_words`:** a change deletes or rewrites content you wrote, as opposed to
  content the agent wrote earlier.
- **`addressed_to_ai`:** the request carries text worded as an instruction to an
  agent.
- **`beyond_the_task`:** the request asks for something the session's goal does not
  name.

Three additions, each of which adds findings and none of which clears an item:

- **You say what is sensitive:** folders, repositories, hosts, projects or records.
  The broker cannot know what matters to you. A request that reaches something you
  marked is high risk by your mark, found and not guessed, with the mark as evidence.
- **Correcting a label:** "this wasn't high risk", or "this should have been". Each
  correction is kept with the reason, and changes no policy rule alone. The
  correction is offered on the decided record, where it does not add to every item's
  reading.
- **An optional prompt-injection detector** that you set up, such as a local
  classifier.
  - The detector can only add a suspected `addressed_to_ai` reason, pointing at the
    flagged line and naming the detector and its version.
  - The detector never lowers risk, removes a reason, or marks an item checked.
  - A local detector comes first. A hosted detector receives the text it checks, so
    setting one up is data leaving: a connection you decide with your passkey.

**A check that the queue stays readable,** before more is added to what an item shows.
The queue fails if items get skimmed. The measures:

- the share of items that are high risk, which should stay small
- how often each risk reason is approved, rejected or returned
- how often items are approved from the list without being opened
- how long an item takes to read

When one reason is approved nearly every time, the control plane proposes changing the
reason, as an item. The control plane never proposes approving such items
automatically.

## Risk analysis by a security tool

Some tools exist to judge the risk of what an AI agent is about to do. The control
plane will offer a waiting item to such a tool, and show you what the tool finds.
[Corridor](https://www.corridor.dev/) is the example: Corridor reviews an agent's
plan, and the code the agent writes, for security problems as the agent works. An
item that carries code or a change, such as a skill to promote, a changeset or a
connector, is the same kind of input.

- **One interface, which names no tool.** The control plane will give an analysis
  tool the item as you see it: what is asked, by which principal and session, the
  facts the broker computed, the code or the diff, and the risk reasons already
  found. Any tool that takes that description and returns findings can be connected:
  Corridor, or another tool that offers a similar service.
- **An item says enough, and no more.** An analysis tool should need nothing beyond
  the item to judge the item, and the item should be short enough to judge quickly.
  The same is true for you, so the rules that keep an item readable serve both
  ([../client-rules.md](../client-rules.md)).
- **A finding is evidence, never a verdict.** A tool's finding will appear on the
  item, labelled with the tool's name and version, beside the broker's facts. A
  finding can add a risk reason. A finding never lowers risk, clears an item or
  decides one: only your passkey decides.
- **Sending an item to a hosted tool is data leaving.** An item can hold your code
  and your data. Connecting an analysis tool will therefore be a connection you
  approve with your passkey, which names the kinds of item the tool receives.
- **With an advisor, or without one.** The advisor can call an analysis tool for the
  items that carry code. A tool with no agent around it will annotate items through
  the same endpoint, under the same separation from the requester.

## Registering an MCP server

Anything running as you can write a registration file, and a server's tool
descriptions become documentation every agent reads. The first registration of a
server will therefore be an item of the kind `server_registration`, showing the tools,
their schemas and the proposed read or write classification. Approval pins the server
id, a hash of the tool schemas, and the classification. A later change to the schemas
is a new item that shows the diff ([server-contract.md](server-contract.md), B5).
The item replaces `config/servers.json`.
[connectors.md](connectors.md) designs the item, as the `connector` kind, which
replaces `server_registration`.

## Smaller additions

- **The resource view** (`GET /resources`): what agents reach, and the grants that
  cover each resource.
- **A session explained in the client:** when a session began, and a way to end the
  session now by revoking every grant the session holds. The control plane needs each
  session's start from the gateway.
- **A request partly covered by grants** comes to the queue with only the uncovered
  parts.
- **A request for a folder and a folder inside it:** refusing the inner folder while
  approving the outer folder protects nothing. The item could say so, or ask for the
  outer folder alone.
- **Waiting a little for your decision,** as an option: the broker could hold a run
  for some seconds, so a decision made while you watch runs the script without a
  rerun. The default stays returning at once.
- **Your never-mount list kept by the control plane,** edited in the web client.
- **Tool notes and skill proposals as items**
  ([agent-guidance.md](agent-guidance.md)).
- **Showing a new passkey wherever you are signed in.** The control plane already
  raises `passkey.added`.
