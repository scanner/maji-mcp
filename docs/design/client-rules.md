# Rules for queue clients

Every decision in maji-mcp is made in a client. The web client is the first client.
An Emacs buffer, a command-line client and a native app are planned
([planned/clients.md](planned/clients.md)). The REST API (`api/openapi.yaml`) says what
a client can ask the control plane and what comes back. These rules say what a client
gives the person using the client: what the client shows, how deciding works, and how
the client is used from the keyboard and with a screen reader. Every client is held to
the rules, and a change that breaks a rule is a bug.

**Status:** the rules are in force. Each section ends with where the web client keeps
the section's rules, and the tests that hold them. The last section lists where the
web client falls short. [glossary.md](glossary.md) defines the terms, and
[control-plane.md](control-plane.md) describes what the rules are rules about.

The rules carry most weight where the control plane can't help:
- **An authenticator shows only the site asking, not what you're approving.** What a
  passkey signs is only as good as the client's rendering of the item.
- **Requests come from agents that may have read injected text.** The client is where
  words an agent wrote are kept from passing for facts.

A client that can't keep a rule for an item, such as an Emacs buffer that can't ask
for a passkey, leaves that item to a client that can.

## 1. What you decide on

1. **What you're asked comes first,** after any reasons it's high risk: the change, the
   question, or for anything else the broker's facts. The facts, kept brief, come
   before the requester's claim, so the requester's words never frame a request before
   you've seen it. A question is the requester's own words, so it is labelled as theirs
   (rule 5). Parts are listed where you decide on them, and the claim doesn't come
   before them either.
2. **A high-risk item says why before anything else,** naming each reason the broker
   gave, what it found, and the evidence on the item it rests on, and says again
   where you decide that the passkey signs it. A reason the broker only suspects is
   marked as suspected. Only high risk is tagged; medium is the usual item, and a tag
   on every item would go unread.
3. **No item is called safe.** An item without the tag is one where none of the
   reasons for high risk was found, not one found safe, so a client never labels an
   item low risk, safe or checked.
4. **Text from an agent is shown as text.** Titles, claims, part labels, notes and diff
   lines are never rendered as markup or followed as links, whatever they contain.
5. **The requester's claim is labelled as the requester's,** with a note that it
   wasn't checked and may repeat text the agent read. So is a question, whose title is
   the question in the requester's words.
6. **What you typed stays behind a disclosure.** The words that led to a request are
   shown when asked for. They clarify the request; they approve nothing.
7. **Every note names its author,** you or the advisor. The advisor's note is advice to
   weigh, shown beside the facts, never in their place.
8. **What the broker flags is flagged in words,** such as "Worth a look" on a fact or a
   diff line, as well as by colour.
9. **A diff names each line as added, removed or unchanged,** in text a screen reader
   reads. A new file, such as a skill's first version, says once that it is new and
   reads as code, since every line would say "added" (`Diff.new`).
10. **An item says who asked, from which session and machine, when, and when it
    lapses.** A session is named in three words made from its key, such as
    `laughing-sparky-daisy`, the same in every client and the gateway's log, with the
    key's first 8 characters beside them, since the key is what its grants are held
    to. Each is set in its own colour, to be found at a glance, and the words say what
    it is. A grant held for a session names it the same way. What the name is, and
    what the characters beside it are, is explained once, with the tags. A session's
    name, and a date or time, moves to the next line whole, never split across two.
11. **What you sign is what you saw.** A decision carries the content hash of the
    item as shown. If the item changed or was decided meanwhile, nothing is decided:
    the client says so and shows the item as it is now.
12. **Every tag and mark is explained in one place, reached from every page,** with
    what it means and, for one that limits what you can do, such as "Decide on its
    own", why.
13. **Each row of a list says when:** when its item was asked, or once it is decided,
    when it was decided, so two items alike can be told apart, and how long ago, kept
    current. How long ago is shown and not read out, as it changes each second; the
    time is read. An item says an agent is waiting on it only while it waits. A row
    also names the session that asked; the start of its key is shown and not read
    out, since the name tells sessions apart by ear, and the item reads both.
14. **An item opens where you chose it.** In a window too narrow for the lists and the
    item side by side, the item grows open in its row, beneath the row, rather than
    below every list. Opening another closes it, and so do its row and a Close at its
    end. After a decision, the next item opens in the decided one's place. The row
    you chose stays where it was, and the page scrolls only as far as shows the item,
    or its top when it's taller than the window. It grows open quickly, and at once
    when the system asks for less motion. Its row's line names its session, so the
    item doesn't show it again, but still says it to screen readers, key and all,
    where it says who asked.

*Web client:* `ItemDetail`, `ItemList`, `FactList`, `DiffView`, `TagHelp` and
`QueueView`, with `riskTag`, `evidence`, `listedAt` and `waitedOn` in
`lib/describe.ts`; Vue's interpolation only, never `v-html`. Held by
`web/tests/ItemDetail.test.ts`, `web/tests/ItemList.test.ts`,
`web/tests/TagHelp.test.ts`, `web/tests/QueueView.test.ts`, `web/tests/queue.test.ts`
and the browser test, which decides an item in a narrow window.

## 2. Deciding

1. **An item offers the actions the control plane lists for it,** named as it names
   them, less any the client can't carry out.
2. **What the client can't do safely, it doesn't offer.**
3. **A key pasted into a Connect item stays in the form until the decision is sent,**
   and is cleared then. The key goes beside the decision, never into it, and never
   into a store, browser storage or an address. The form says where the key goes and
   how it is sent, what it must be allowed, and links to where you make one. The
   decision names the key's SHA-256, made in the browser with WebCrypto, which needs
   `localhost` or TLS, as passkeys do.
4. **An action that needs something asks for it before anything is sent:** a note to
   return an item or ask for changes to a skill, an answer to a question, a time to
   remember an approval until. The note's label names the actions that need it.
4. **A request with parts lists every part, each approved unless you untick it.**
   Approving every part is the default; parts are for refusing some.
5. **After a decision, focus moves to the next item's heading, and its title is read
   out.** Focus is never left on a button, which now belongs to the next item, and the
   next item's buttons stay inert for a moment. Neither a second key press nor a double
   click decides an item you haven't read.

*Web client:* `DecisionForm` and `QueueView`. Held by `web/tests/ItemDetail.test.ts`
and the browser test, which checks focus after every decision.

## 3. Batches

1. **A batch is for the easy, clear items.** It approves everything each item asks for,
   just this once, with one signature over all their hashes.
2. **An item with parts, an approval that can be remembered, or high risk is decided on
   its own.** In a batch you couldn't refuse one of its parts, choose to remember it,
   or give a high-risk request its own look.
3. **The client says why an item can't join a batch.** The list marks it "Decide on its
   own", its details give the reason, and the batch key gives it too, instead of doing
   nothing. Questions and connections need more than an approval, and their kind says
   so.
4. **The batch says what it does before you approve it:** how many items, and that one
   approval covers them all, each as you saw it.

*Web client:* `batchable` and `aloneBecause` in `stores/queue.ts`, `ItemList` and
`BatchBar`. Held by `web/tests/ItemList.test.ts`, `web/tests/ItemDetail.test.ts` and the
browser test.

## 4. Remembering an approval

1. **The shortest duration is the default:** just this once, or for a mount, which is
   never just once, this session, or 8 hours when the requester named no session.
   Remembering for longer is always chosen, never assumed. A mount can also be kept
   for the session but 8 hours at most, which you choose; the session is the default
   as you set it (2026-11-09).
2. **Each pattern is offered beside what it covers:** a pattern over one part beside
   that part, and patterns over the whole request, or several parts, after the parts.
3. **Only durations a grant can be held to are offered:** the rest of the run or the
   session only when the requester named one, and a chosen time only in the future.
4. **A part you refuse can't be remembered,** and its choice is disabled as you untick
   it.
5. **The client says when a decision will ask for your passkey,** before you press the
   button.
6. **A session is said for what it is:** whose it is, which it is, when it began,
   that it lasts until the agent's app closes or reconnects, or the agent goes an hour
   without using maji-mcp, and how to end it. Where a choice says "For this session",
   the session is named once beside the choices, quietly, so it doesn't draw the eye
   from what each grant covers, and read with each choice. A request from an app that
   named no session says so, and that a mount is kept for a time instead.
7. **One choice sets how long every part is remembered,** when a request has several
   parts that can each be remembered. The choice starts where every part starts, at
   the shortest duration, and offers only the durations every part offers. Each
   part's own choice still changes that part alone, and the one choice then says the
   parts differ. A part you refuse is left as it was.

*Web client:* `DecisionForm`. Held by `web/tests/ItemDetail.test.ts`, which checks
the durations offered, the one choice for every part, and that a time past is
refused, and by the browser test.

## 5. Signing

1. **Every decision is signed by you,** with a passkey, or with a key your passkey
   unlocked in this tab. A token never decides.
2. **An unlocked tab's key stays in the page.** It can't be exported, is never stored,
   and is gone when the page goes.
3. **Unlocking lasts while you use the tab.** It locks after the control plane's idle
   limit, at sign-out, when you press Lock, or when the page reloads, and the Passkeys
   page turns it off.
4. **The client shows that deciding is unlocked and how long is left,** and warns once
   in the last minute, on screen and to a screen reader.
5. **Some decisions take the passkey even in an unlocked tab:** remembering an
   approval until a time or always, connecting a service, and deciding a high-risk
   item. A session's grant that ends at a time doesn't, as it covers no more than the
   session would. Nor does taking a decision back, such as demoting a skill, even on a
   high-risk item: it carries a note at most, and only narrows what agents can do.

*Web client:* `api/tabkey.ts`, `stores/unlock.ts` and the header in `App.vue`. Held by
`web/tests/tabkey.test.ts`, `web/tests/unlock.test.ts`, `web/tests/queue.test.ts` and the
browser test, which counts the passkey's signatures.

## 6. What was decided

1. **A decided item says what was decided, by you or by which rule, and when.**
2. **It says how the decision was signed:** with your passkey, or in a tab your passkey
   unlocked.
3. **It says what became of each part,** approved or refused and how long each is
   remembered, and what was remembered over the whole request, the answer given to a
   question, your note, and whether the decision has been applied.
4. **History is read-only.** Changing your mind is a new decision, such as revoking a
   grant, never an edit of the old one.
5. **A refused mount offers to let the agent ask again,** saying that until then it
   can't, and once you have, when. It grants nothing.
6. **A promotion still in effect offers to demote the skill,** below the record of it,
   saying what demoting does: no agent runs that version, the one that kept it
   included, and each that tries is told why, with your note. The demotion is a new
   decision naming the promotion; the item says what it took back, stays open with
   focus on it, and moves to the top of the history (2026-11-11).

*Web client:* `ItemDetail`, `DecisionForm`, and `decided`, `DECIDED` and
`refusedMounts` in `lib/describe.ts`. Held by `web/tests/ItemDetail.test.ts` and the
browser test.

## 7. Keeping up

1. **The queue stays current.** The client follows the event stream, and lists the
   items again when the stream says it can't resume.
2. **What happens is said, not only shown.** Results are announced politely and
   failures assertively, and a burst of updates is read as one announcement.
3. **A new passkey is shown wherever you are signed in** (`passkey.added`). Anything
   running as you could add one, so the client makes it visible.

*Web client:* `stores/queue.ts` and `stores/announcer.ts`. Rule 3 isn't kept yet.

## 8. Keyboard, screen readers, colour and type

1. **Every control is reached and used from the keyboard in the expected way.** Tab and
   Shift-Tab move between the page's parts and an item's fields. Each list is one stop,
   moved through with the arrows, Home and End, and C-n, C-p, M-< and M-> as in Emacs.
   An item open in its row (1.14) has its fields after its row in the Tab order; the
   list's keys move only between rows.
2. **Single-key shortcuts can be turned off,** since they can clash with a screen
   reader's own keys. The keyboard help lists each action once, with every key that
   does it, and the single keys only while they're on.
3. **Each page moves focus to its heading,** and has landmarks and a skip link.
4. **Meaning is never carried by colour alone.** Risk, state and flags are words. The
   palette comes from the Okabe-Ito set, for the common kinds of colour blindness, and
   every pair of text and background has a contrast of 4.5:1 or more, in the light
   and dark themes.
5. **In the type, `1`, `l`, `I`, `O` and `0` can't be mistaken for each other,** and code
   is set in the same family as the text around it.
6. **Native controls come first:** buttons, checkboxes, selects and dialogs, which
   assistive technology already knows.

*Web client:* `ItemList`, `KeyboardHelp`, `styles/tokens.css`, and Atkinson Hyperlegible
Next and Mono, bundled. Held by axe-core in `web/tests/ItemList.test.ts`,
`web/tests/ItemDetail.test.ts` and `web/tests/KeyboardHelp.test.ts`, colour contrast in
`web/tests/contrast.test.ts`, and axe with real contrast on every page in the browser
test. Not yet tried: Safari, Firefox and VoiceOver.

## 9. Passkeys and signing in

1. **The setup code leaves the address bar** as soon as the page has read it.
2. **Recovery codes are shown once,** when the first passkey is made.
3. **Every passkey is listed with its name, when it was added and when it was last
   used.** The last one can't be removed, and the client says why.
4. **After signing in, the client returns you only to a page on its own site.**
5. **Signing out locks deciding in the tab.**

*Web client:* `SignInView` and `PasskeysView`, with the setup page the control plane
serves (`src/maji_mcp/control/pages/setup.html`). Held by the browser test.

## 10. Grants

1. **A grant is found by any fragment of what it covers or who holds it,** with the
   matching characters marked.
2. **Grants are revoked one at a time, or a group at once,** grouped by what they
   cover, who holds them, their access, how long they last, or the session that holds
   them, named as a session is everywhere. Only a grant that lasts for its session
   is held by it: one made in it that lasts longer is not, and is never revoked with
   the session's. Grants without what a group shares, such as those held by no
   session, are revoked one at a time.

*Web client:* `GrantsView` and `stores/grants.ts`. Held by `web/tests/grants.test.ts`
and the browser test.

## 11. The page itself

1. **The page runs only its own code.** Its content security policy allows scripts,
   styles and fonts from its own origin only, with no inline script, and no other site
   may frame it.
2. **Nothing is fetched from another site,** and no referrer is sent.

*Web client:* the headers set in `src/maji_mcp/control/api.py`. Held by the browser
test, which fails on any policy violation.

## Where the web client falls short

- **A request's parts come after its claim** (1.1). They are listed in the decision
  form, below the requester's claim.
- **A session is explained only in part** (4.6). It is named (1.10), but the client
  doesn't say when a session began, or offer to end one.
- **A grant isn't found by its session's name** (10.1). Grants can be grouped by
  session (10.2), but a search matches only what a grant covers and who holds it.
- **A new passkey isn't shown** (7.3). The control plane raises `passkey.added`, and
  the web client doesn't act on it yet.
- **The compact view** from the mockup ([mockups/decision-queue.html](mockups/decision-queue.html))
  is built only in part. A narrow window opens an item in its row (1.14), but each
  row still takes several lines, not one.
- **Safari, Firefox and VoiceOver** haven't been tried.
