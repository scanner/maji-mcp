# Planned: more clients for the queue

The clients that will follow the web client. Every client talks only to the control
plane's REST API, and follows the same rules ([../client-rules.md](../client-rules.md)),
so clients are interchangeable.

**Status:** planned. The web client is the only client built. The control plane
already issues a client token for a command-line client: a client token reads the
queue and narrows access, and never decides.

## The web client's own gaps

[../client-rules.md](../client-rules.md), "Where the web client falls short", lists
them. The largest is the compact view: one line for each waiting item, the item you
are deciding open in place, and the history collapsed at the bottom.
[../mockups/decision-queue.html](../mockups/decision-queue.html) is a clickable mockup
of the full view and the compact view, with sample data. Safari, Firefox and VoiceOver
have not been tried.

## An Emacs client

A magit-style queue buffer, which fits work done in Emacs.

- The buffer lists the queue and opens an item.
- An emacs-org changeset opens in ediff.
- The buffer approves and rejects items that need no passkey, and hands an item that
  needs a passkey to a client that can produce an assertion.

The Emacs client comes after writes as changesets ([writes.md](writes.md)).

## A command-line client

For scripting, and for signing with an SSH key (`ssh-keygen -Y sign`) once the trust
set takes SSH keys ([sharing.md](sharing.md)).

## A macOS app

Two pieces, after the web client:

- **A menu bar panel that holds the compact queue view.** SwiftUI's `MenuBarExtra`, in
  window style, can host a panel of this size. The panel talks to the same API and
  follows the event stream. The panel uses the platform's passkey support, which
  requires the app to be tied to the same relying-party domain as the control plane.
- **A widget for a quick look:** the counts, what is blocking an agent, and the next
  item. Selecting the widget opens the panel on that item. A widget cannot hold the
  panel itself: a widget refreshes from a timeline, has no text input or scrolling,
  and runs its buttons in the background with no way to show a passkey prompt. These
  limits come from WidgetKit as of macOS 14, and need checking against the current
  version when building.

## Open questions for every client

- **An authenticator shows only the site that asks,** never the content being
  approved. The signature is only as good as the client's display of the item.
- **A passkey is bound to a relying-party ID.** Every instance's client needs to be
  served under the same ID, or each machine's passkey has to be registered with each
  instance.
