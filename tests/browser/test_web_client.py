"""
The web client in a real browser: your installed Chrome, with a virtual passkey.

A control plane serves the built client (`make web`) and a demo queue. Chrome's
DevTools protocol supplies a virtual authenticator, so the client's own passkey code
runs end to end: the setup page, signing in, and deciding. Every page is held to the
control plane's content security policy; a violation fails the test. axe-core checks
each page in the real browser, where colour contrast is computed.

    make test-browser
"""

# system imports
import asyncio
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

# 3rd party imports
import pytest
import uvicorn
from playwright.sync_api import CDPSession, Page, expect, sync_playwright
from pytest_check import check

# project imports
from maji_mcp.common.names import session_name
from maji_mcp.control.api import Settings, build_app
from maji_mcp.control.demo import AGENT, seed
from maji_mcp.control.server import setup_code
from maji_mcp.control.store import ControlStore
from tests.support.paths import REPO

pytestmark = pytest.mark.browser

DIST = REPO / "web" / "dist"

# A made-up key, pasted into the demo's Connect item.
#
PASTED_KEY = "ATATT3xFfGF0-made-up-key-for-the-browser-test"
DEMO_SESSION = str(AGENT["session"])
AXE = REPO / "web" / "node_modules" / "axe-core" / "axe.min.js"


@dataclass
class Visitor:
    """
    A person at the control plane in Chrome, holding a passkey device.

    Attributes:
        page: the tab they are looking at
        devtools: Chrome's DevTools session for the tab, which reaches the device
        authenticator: the id of the virtual passkey device
        refused: what the tab's console said its content security policy refused,
            which stays empty while every page holds to the policy
    """

    page: Page
    devtools: CDPSession
    authenticator: str
    refused: list[str]

    def focused_heading(self) -> str:
        """The text of the focused element, which must be a heading."""
        found = self.page.evaluate(
            "document.activeElement.matches('[data-item-heading], "
            "[data-page-heading]') && document.activeElement.textContent"
        )
        assert found, "after a decision, focus must be on a heading"
        return str(found)

    def touches(self) -> int:
        """
        How many times the passkey has signed. The virtual authenticator confirms
        presence by itself, so its counter is how a test sees each prompt.
        """
        found = self.devtools.send(
            "WebAuthn.getCredentials", {"authenticatorId": self.authenticator}
        )
        return int(found["credentials"][0]["signCount"])

    def check_accessible(self) -> None:
        """
        Check the page as it is now with axe, and go on whatever axe finds.

        Each page is checked on its own, so one run names the violations on every
        page rather than stopping at the first page that has one.
        """
        found = self.page.evaluate("axe.run(document, {resultTypes: ['violations']})")
        violations = [f"{v['id']}: {v['help']}" for v in found["violations"]]
        with check:
            assert violations == [], self.page.url


@pytest.fixture(scope="module")
def plane(unused_tcp_port_factory: Callable[[], int]) -> Iterator[tuple[str, str]]:
    """
    A control plane serving the built client and a seeded demo queue, as its origin
    and the setup code for its first passkey. Without a built client, the test is
    skipped.
    """
    if not (DIST / "index.html").exists() or not AXE.exists():
        pytest.skip("build the web client first (make web)")
    port = unused_tcp_port_factory()
    origin = f"http://localhost:{port}"
    ready = threading.Event()
    shared: dict[str, Any] = {}

    # The store's SQLite connection belongs to the thread that made it, so the
    # control plane, its store and its event loop all live in one thread of their own.
    #
    def run() -> None:
        async def main() -> None:
            store = ControlStore(":memory:", "demo")
            seed(store)
            shared["code"] = setup_code(store)
            settings = Settings(origin=origin, web_dir=DIST)
            server = uvicorn.Server(
                uvicorn.Config(
                    build_app(store, settings),
                    host="127.0.0.1",
                    port=port,
                    log_level="warning",
                )
            )
            shared["server"] = server
            serving = asyncio.create_task(server.serve())
            while not server.started:
                await asyncio.sleep(0.05)
            ready.set()
            await serving

        asyncio.run(main())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(20), "the control plane never started"
    yield origin, shared["code"]
    shared["server"].should_exit = True
    thread.join(10)


@pytest.fixture
def visitor() -> Iterator[Visitor]:
    """
    A person in your installed Chrome, with axe loaded into every page and a virtual
    passkey device that confirms presence by itself. Without Chrome, the test is
    skipped.
    """
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # noqa: BLE001 - no Chrome installed
            pytest.skip(f"Chrome isn't available: {exc}")
        context = browser.new_context()
        context.add_init_script(path=str(AXE))
        page = context.new_page()
        refused: list[str] = []
        page.on(
            "console",
            lambda message: (
                refused.append(message.text)
                if "Content Security Policy" in message.text
                or "Refused" in message.text
                else None
            ),
        )
        devtools = context.new_cdp_session(page)
        devtools.send("WebAuthn.enable")
        authenticator = devtools.send(
            "WebAuthn.addVirtualAuthenticator",
            {
                "options": {
                    "protocol": "ctap2",
                    "transport": "internal",
                    "hasResidentKey": True,
                    "hasUserVerification": True,
                    "isUserVerified": True,
                    "automaticPresenceSimulation": True,
                }
            },
        )
        try:
            yield Visitor(page, devtools, authenticator["authenticatorId"], refused)
        finally:
            browser.close()


def test_a_person_sets_up_signs_in_and_decides(
    plane: tuple[str, str], visitor: Visitor
) -> None:
    """
    GIVEN: A new control plane with a demo queue, and Chrome with a passkey device
     WHEN: a person goes through the client from the setup link to signing out:
           creating a passkey, signing in, deciding each kind of item in the queue,
           taking a decision back from the history, deciding in a narrow window,
           locking deciding, revoking grants, and opening the passkeys and the help
     THEN: Setup: the setup code leaves the address bar, and the passkey is created
           with ten recovery codes
      AND: Signing in: the queue sends a signed-out browser to sign in, then back
      AND: The waiting list is one stop in the Tab order: C-n moves through it, and
           one Tab leaves it
      AND: A tick, which can be remembered, says it is decided on its own, not in a
           batch. Approving it takes one passkey touch, which unlocks deciding in
           the tab
      AND: Two more items are approved as a batch, and a question is answered from
           its choices, with no further touch
      AND: A request for three mounts is approved with one refused and the others
           kept for the session, with no further touch. Once decided, it says which
           repositories were approved, refused and remembered, and lets the agent
           ask again for the one refused
      AND: A connection, which lasts, says where its key goes, and takes its own
           touch to connect with a pasted key, which is gone from the page once
           sent; the Connections page lists the service
      AND: A skill that writes says why it is high risk first, pointing at what it
           writes with, shows its code as a new file, won't take changes asked for
           without a note, and takes its own touch to promote
      AND: Demoting the skill from the history takes no touch, says what it took
           back, and leaves focus on it, at the top of the history
      AND: In a narrow window an item opens in its row, in view. It shows its session
           only in its row's line, but says it to screen readers where it says who
           asked. A high-risk change says why first, marked as suspected, with the
           line it rests on, and takes its own touch. The next item opens in the
           decided one's place, with focus on its heading, and closes from its row
           or from its end, with focus back on its row
      AND: Locking takes the tab's key away
      AND: A grant is found by a fragment of its name, names the session it is held
           for in words, with the start of its key, and is revoked. Grouped by
           session, that session's grants are revoked together
      AND: The last passkey can't be removed, and the page says why
      AND: The keyboard help and what the tags mean open and close, and signing out
           works
      AND: Throughout: after each decision focus is on a heading, never left on a
           button that now belongs to the next item; every page holds to its content
           security policy; and axe finds no accessibility violations on the setup
           page, the sign-in page, the queue, the grants, the passkeys or the tags'
           meanings
    """
    origin, code = plane
    page = visitor.page

    # The setup page registers the first passkey with the code from the link.
    #
    page.goto(f"{origin}/setup#code={code}")
    expect(page.get_by_role("button", name="Create a passkey")).to_be_visible()
    assert "code=" not in page.url
    visitor.check_accessible()
    page.get_by_label("Name for this passkey").fill("browser test")
    page.get_by_role("button", name="Create a passkey").click()
    expect(page.get_by_role("heading", name="Save your recovery codes")).to_be_visible()
    expect(page.locator("#code-list li")).to_have_count(10)

    # The queue sends a signed-out browser to sign in, then back.
    #
    page.goto(f"{origin}/queue")
    expect(page.get_by_role("heading", name="Sign in", level=1)).to_be_visible()
    visitor.check_accessible()
    page.get_by_role("button", name="Sign in with a passkey").click()
    expect(page.get_by_role("heading", name="Queue", level=1)).to_be_visible()
    expect(page.get_by_role("heading", name=re.compile("^Waiting"))).to_be_visible()
    visitor.check_accessible()

    # The waiting list is one stop in the Tab order: C-n moves through it, and one
    # Tab leaves it.
    #
    waiting = page.get_by_role("list", name="Waiting for a decision")
    waiting.get_by_role("button").first.focus()
    page.keyboard.press("Control+n")
    expect(waiting.get_by_role("button").nth(1)).to_be_focused()
    page.keyboard.press("Tab")
    assert page.evaluate(
        "!document.activeElement.closest('[aria-label=\"Waiting for a decision\"]')"
    )

    # A tick can be remembered, so it is decided on its own rather than in a
    # batch, and says so. Approved with one touch, which also unlocks deciding in
    # the tab.
    #
    signed_in = visitor.touches()
    tick = waiting.get_by_role("button", name=re.compile("Tick"))
    expect(tick).to_contain_text("Decide on its own")
    tick.click()
    expect(page.locator(".alone")).to_contain_text("can be remembered")
    page.get_by_role("button", name="Approve", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("Approved")
    assert "Tick" not in visitor.focused_heading()
    expect(page.get_by_text("Deciding unlocked")).to_be_visible()

    # Two more, as a batch.
    #
    page.get_by_label(re.compile("Select for a batch approval: Add a note")).check()
    page.get_by_label(re.compile("Select for a batch approval: Create task")).check()
    page.get_by_role("button", name="Approve 2 items").click()
    expect(page.get_by_role("status")).to_contain_text("2 decisions recorded")
    visitor.focused_heading()

    # A question, answered from its choices.
    #
    page.get_by_role(
        "button", name=re.compile("Which repository holds the billing work")
    ).click()
    page.get_by_label("~/src/billing").check()
    page.get_by_role("button", name="Answer", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("Answered")
    visitor.check_accessible()

    # A request to mount three paths: one refused, one remembered for the session,
    # as a mount is unless you choose, and one for the session but 8 hours at
    # most. Neither covers more than the session, so the unlocked tab decides it
    # without a passkey touch.
    #
    page.get_by_role("button", name=re.compile("Mount 3 paths")).click()
    expect(page.get_by_text("A mount is never approved just once")).to_be_visible()
    expect(page.get_by_text("A session lasts until the agent's app")).to_be_visible()
    page.get_by_label("~/Documents/notes", exact=True).uncheck()
    expect(
        page.get_by_label("Remember Read-only mounts of ~/Documents/notes")
    ).to_be_disabled()
    expect(page.get_by_label("Remember Read-only mounts of ~/src/api")).to_have_value(
        "session"
    )
    page.get_by_label("Remember Read-only mounts of ~/src/web").select_option("hours")
    visitor.check_accessible()
    page.get_by_role("button", name="Grant", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("Approved")
    assert visitor.touches() == signed_in + 1

    # Once decided, it says what became of each repository, and lets the agent
    # ask again for the one refused, once you've told it more.
    #
    page.get_by_role("button", name=re.compile("^Decided")).click()
    page.locator("#history-list").get_by_role(
        "button", name=re.compile("Mount 3 paths")
    ).click()
    asked = page.get_by_role("list", name="What it asked for")
    expect(asked).to_contain_text("~/src/api: approved, remembered for this session.")
    expect(asked).to_contain_text(
        re.compile(
            r"~/src/web: approved, remembered for this session, until .+ at the latest"
        )
    )
    expect(asked).to_contain_text("~/Documents/notes: refused")
    visitor.check_accessible()
    page.get_by_role("button", name="Let it ask again").click()
    expect(page.get_by_role("status")).to_contain_text("The agent can ask again")
    expect(page.locator(".record")).to_contain_text(
        "You let the agent ask again for what you refused"
    )
    assert visitor.touches() == signed_in + 1

    # A connection lasts beyond the run, so it asks for the passkey even in an
    # unlocked tab. The key is pasted into the item, says where it goes, and is gone
    # from the page once the decision is sent.
    #
    page.get_by_role("button", name=re.compile("Connect Jira")).click()
    expect(page.get_by_text("so your passkey signs it")).to_be_visible()
    expect(page.get_by_text("sends it only to api.atlassian.com")).to_be_visible()
    visitor.check_accessible()
    page.get_by_label("Jira key", exact=True).fill(PASTED_KEY)
    page.get_by_label("Site", exact=True).fill("acme")
    page.get_by_label("Email", exact=True).fill("me@example.com")
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("Key sent")
    assert visitor.touches() == signed_in + 2
    assert PASTED_KEY not in page.content()

    # Promoting a skill that writes lets any agent run it, so it is high risk: it
    # says why first, pointing at what it writes with, shows the code as the new
    # file it is, and takes the passkey even in an unlocked tab. Asking for
    # changes would need a note for the agent.
    #
    page.get_by_role("button", name=re.compile("Promote skills.tick_item")).click()
    risk = page.locator(".risk")
    expect(risk).to_contain_text("Trust changes for later")
    expect(risk).to_contain_text("Writes with: org.update_task")
    expect(page.get_by_role("heading", name="The code")).to_be_visible()
    expect(page.locator("figcaption")).to_have_text(
        "New file skills/me/local/tick_item/skill.py"
    )
    visitor.check_accessible()
    page.get_by_role("button", name="Request changes", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text(
        "Request changes needs a note for the requester"
    )
    expect(
        page.get_by_label("Note to the requester (optional, except to ask for changes)")
    ).to_be_visible()
    page.get_by_role("button", name="Promote and sign", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("Promoted")
    assert visitor.touches() == signed_in + 3

    # Once decided, the promotion can be taken back from the history: demoting
    # only narrows what agents can do, so the unlocked tab signs it without a
    # touch, and the item stays open, with focus on it, now at the top of the
    # history.
    #
    history = page.locator("#history-list")
    history.get_by_role("button", name=re.compile("Promote skills.tick_item")).click()
    record = page.locator(".now .record")
    expect(record).to_contain_text("Promoted by you")
    expect(record).to_contain_text(
        "Demoting stops every agent running this version, the agent that kept it too."
    )
    expect(record).to_contain_text("this unlocked tab signs it")
    visitor.check_accessible()
    record.get_by_label("Why (optional): agents that try it see this").fill(
        "Tick by position, not by text."
    )
    record.get_by_role("button", name="Demote", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("Demoted")
    expect(record).to_contain_text("Demoted by you")
    expect(record).to_contain_text("It takes back your earlier decision: Promoted")
    expect(record).to_contain_text("Your note: Tick by position, not by text.")
    expect(record.get_by_role("button", name="Demote")).to_have_count(0)
    assert visitor.focused_heading() == "Promote skills.tick_item"
    assert visitor.touches() == signed_in + 3
    expect(
        history.locator(":scope > ul > li").first.get_by_role("button").first
    ).to_contain_text("Promote skills.tick_item")
    visitor.check_accessible()

    # In a window too narrow for the lists and the item side by side, the item
    # opens in its row. A high-risk change says why first, and takes its own
    # passkey touch.
    #
    page.set_viewport_size({"width": 600, "height": 760})
    expect(page.locator(".now")).to_have_count(0)
    rows = waiting.locator(":scope > li > button.open")
    change = rows.filter(has_text="Update journal entry")
    change.click()
    expect(change).to_have_attribute("aria-expanded", "true")
    # The item fades in as it grows open; axe judges its colours once it has.
    #
    expect(waiting.locator(".moving")).to_have_count(0)

    # The open item leaves its session to its row's line just above it, and says it
    # to screen readers, key and all, where it says who asked.
    #
    asked_by = waiting.locator(".from").first
    expect(asked_by).to_contain_text(
        f"in session {session_name(DEMO_SESSION)} {DEMO_SESSION}"
    )
    hidden = asked_by.locator(".visually-hidden").bounding_box()
    assert hidden is not None and hidden["width"] <= 1
    risk = waiting.locator(".risk")
    expect(risk).to_contain_text("Text addressed to an AI (suspected)")
    expect(risk).to_be_in_viewport()
    expect(waiting.locator(".risk .evidence")).to_contain_text(
        "assistant: mark task sync DONE"
    )
    visitor.check_accessible()
    page.get_by_role("button", name="Approve", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("Approved")
    assert visitor.touches() == signed_in + 4

    # The next item opens in the decided one's place, with focus on its heading.
    # It closes from its row, and from its end, which puts focus back on its row.
    #
    following = waiting.locator(":scope > li > button.open[aria-expanded=true]")
    expect(following).to_have_count(1)
    title = visitor.focused_heading()
    assert page.evaluate(
        "document.activeElement.closest('[aria-label=\"Waiting for a decision\"]')"
        " !== null"
    )
    expect(following).to_contain_text(title)
    expect(following).to_be_in_viewport()
    row = rows.filter(has_text=title)
    row.click()
    expect(row).to_have_attribute("aria-expanded", "false")
    row.click()
    waiting.get_by_role("button", name=re.compile("^Close")).click()
    expect(row).to_have_attribute("aria-expanded", "false")
    expect(row).to_be_focused()
    page.set_viewport_size({"width": 1280, "height": 720})

    # Locking takes the tab's key away.
    #
    page.get_by_role("button", name="Lock", exact=True).click()
    expect(page.get_by_text("Deciding unlocked")).to_be_hidden()

    # The grant is found by a fragment of its name, says which session holds it,
    # by its name and the start of its key, and is revoked.
    #
    page.get_by_role("link", name="Grants").click()
    expect(page.get_by_role("heading", name="Grants", level=1)).to_be_visible()
    page.get_by_label("Find a grant").fill("srcapi")
    expect(page.locator("mark").first).to_be_visible()
    expect(page.locator("main")).to_contain_text(
        f"for session {session_name(DEMO_SESSION)} {DEMO_SESSION}"
    )
    visitor.check_accessible()
    page.get_by_role("button", name="Revoke Read-only mounts of ~/src/api").click()
    expect(page.get_by_role("status")).to_contain_text("Revoked")

    # Grouped by the session that holds them, the session's other grant, for
    # ~/src/web, is revoked with its group.
    #
    named = session_name(DEMO_SESSION)
    page.get_by_label("Find a grant").fill("")
    page.get_by_label("Group by").select_option("session")
    group = page.locator(".groups > li").filter(has_text=named)
    expect(group).to_contain_text("1 grant")
    visitor.check_accessible()
    group.get_by_role("button", name=re.compile("^Revoke all")).click()
    group.get_by_role("button", name="Revoke them").click()
    expect(page.get_by_role("status")).to_contain_text(
        f"Revoked 1 grant for session {named}"
    )
    expect(page.locator(".groups > li").filter(has_text=named)).to_have_count(0)

    # The Connections page lists the service. The demo has no broker, so nothing
    # took the key, and Jira stays unconnected.
    #
    page.get_by_role("link", name="Connections").click()
    expect(page.get_by_role("heading", name="Connections", level=1)).to_be_visible()
    expect(page.locator("main")).to_contain_text("Jira")
    expect(page.locator("main")).to_contain_text("Not connected")
    visitor.check_accessible()

    # The last passkey can't be removed, and the page says why.
    #
    page.get_by_role("link", name="Passkeys").click()
    expect(page.get_by_role("heading", name="Passkeys", level=1)).to_be_visible()
    visitor.check_accessible()
    page.get_by_role("button", name="Remove the passkey browser test").click()
    expect(page.get_by_role("alert")).to_contain_text("last passkey")

    # The keyboard help opens and closes, under the page's policy.
    #
    page.get_by_role("button", name="Keyboard").click()
    expect(page.get_by_role("dialog", name="Keyboard")).to_be_visible()
    page.get_by_role("button", name="Close").click()
    expect(page.get_by_role("dialog", name="Keyboard")).to_be_hidden()

    # So does what the tags mean.
    #
    page.get_by_role("button", name="Tags").click()
    tags = page.get_by_role("dialog", name="What the tags mean")
    expect(tags).to_contain_text("Decide on its own")
    visitor.check_accessible()
    tags.get_by_role("button", name="Close").click()
    expect(tags).to_be_hidden()

    # Signing out, the one DELETE the client sends, carries the Origin it needs.
    #
    page.get_by_role("button", name="Sign out").click()
    expect(page.get_by_role("heading", name="Sign in", level=1)).to_be_visible()

    assert visitor.refused == []
