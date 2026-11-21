/**
 * The item in front of you: agent text shown as text, a diff that doesn't rely on
 * colour, and decisions built from what was chosen.
 */
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/client";
import DecisionForm from "@/components/DecisionForm.vue";
import ItemDetail from "@/components/ItemDetail.vue";
import { afterSlashes } from "@/lib/describe";

import { violations } from "./axe";
import { item } from "./fixtures";

describe("the item detail", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
  });

  it("shows a claim holding markup as text, never as markup", () => {
    const detail = mount(ItemDetail, {
      props: { item: item({ claim: '<img src=x onerror="alert(1)">' }), busy: false },
    });
    expect(detail.find("blockquote").text()).toBe('<img src=x onerror="alert(1)">');
    expect(detail.find("img").exists()).toBe(false);
  });

  it("shows what the requester's app reported itself as, in brackets after who asked", () => {
    const said = mount(ItemDetail, {
      props: {
        item: item({
          requester: { principal: "claude-code", session: "s-1", client: { name: "claude-code", version: "2.1.318" } },
        }),
        busy: false,
      },
    });
    expect(said.find(".from").text().replace(/\s+/g, " ")).toContain(
      "From claude-code (claude-code 2.1.318) in session s-1",
    );
    const silent = mount(ItemDetail, { props: { item: item(), busy: false } });
    expect(silent.find(".from").text()).not.toContain("(");
  });

  it("names the requester's session in words, with the start of its key beside them", () => {
    const key = "3f9a61c2" + "0".repeat(56);
    const named = mount(ItemDetail, {
      props: {
        item: item({ requester: { principal: "claude-code", session: key }, session_name: "laughing-sparky-daisy" }),
        busy: false,
      },
    });
    const from = named.find(".from");
    expect(from.text().replace(/\s+/g, " ")).toContain("in session laughing-sparky-daisy 3f9a61c2,");
    expect(from.find("code").text()).toBe("3f9a61c2");
    expect(from.text()).not.toContain(key);
    expect(from.find(".visually-hidden").exists()).toBe(false);
  });

  it("names each diff line as added, removed or unchanged for screen readers", () => {
    const detail = mount(ItemDetail, {
      props: {
        item: item({
          diff: {
            target: "tasks.org",
            lines: [
              { op: "delete", text: "- [ ] Buy the paint" },
              { op: "add", text: "- [X] Buy the paint" },
            ],
          },
        }),
        busy: false,
      },
    });
    const said = detail.findAll("th .visually-hidden").map((n) => n.text());
    expect(said).toEqual(["Removed:", "Added:"]);
  });

  it("shows a skill's new code as code: named new once, not marked line by line", async () => {
    const detail = mount(ItemDetail, {
      props: {
        item: item({
          kind: "promote",
          title: "Promote skills.keys",
          diff: {
            target: "skills/me/local/keys/skill.py",
            new: true,
            lines: [
              { op: "add", text: "def keys() -> list:" },
              { op: "add", text: "    return eval('x')", flag: true },
            ],
          },
          actions: [
            { id: "promote", label: "Promote and sign", style: "primary" },
            { id: "request_changes", label: "Request changes", style: "neutral", needs: ["note"] },
            { id: "reject", label: "Reject", style: "danger" },
          ],
        }),
        busy: false,
      },
      attachTo: document.body,
    });
    expect(detail.find(`#diff-${item().id}`).text()).toBe("The code");
    expect(detail.find("figcaption").text()).toBe("New file skills/me/local/keys/skill.py");
    expect(detail.find("caption").text()).toBe(
      "Every line of skills/me/local/keys/skill.py, all of it new",
    );
    expect(detail.findAll("th.mark")).toHaveLength(0);
    expect(detail.findAll("td .visually-hidden").map((n) => n.text())).toEqual(["Worth a look:"]);
    expect(detail.find(".note label").text()).toBe(
      "Note to the requester (optional, except to ask for changes)",
    );
    expect(await violations(detail.element)).toEqual([]);
  });

  it("shows lines only added to a file that exists as a change to it", () => {
    const detail = mount(ItemDetail, {
      props: {
        item: item({ diff: { target: "tasks.org", lines: [{ op: "add", text: "** TODO Paint" }] } }),
        busy: false,
      },
    });
    expect(detail.find("figcaption").text()).toBe("Change to tasks.org");
    expect(detail.findAll("th .visually-hidden").map((n) => n.text())).toEqual(["Added:"]);
  });

  it("says which action needs the note, and that none does when none does", () => {
    const label = (actions: Item["actions"]) =>
      mount(ItemDetail, { props: { item: item({ actions }), busy: false } })
        .find(".note label")
        .text();
    expect(label(item().actions)).toBe("Note to the requester (optional, except to return it)");
    expect(label([{ id: "approve", label: "Grant", style: "primary" }])).toBe(
      "Note to the requester (optional)",
    );
  });

  it("passes axe with facts, a diff, a claim and the decision form", async () => {
    const detail = mount(ItemDetail, {
      props: {
        item: item({
          claim: "Adds the notes.",
          words: "add the notes",
          diff: { target: "a.org", lines: [{ op: "add", text: "x" }] },
          parts: [{ id: "api", label: "~/src/api" }],
          patterns: [{ id: "p", summary: "Mounts of ~/src/api", access: "mount", match: {} }],
        }),
        busy: false,
      },
      attachTo: document.body,
    });
    expect(await violations(detail.element)).toEqual([]);
  });
});

describe("what you're asked", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
  });

  const headings = (detail: ReturnType<typeof mount>) => detail.findAll("h3").map((h) => h.text());

  it("comes first for a question: the question is its heading, in the requester's own words", async () => {
    const detail = mount(ItemDetail, {
      props: {
        item: item({
          kind: "question",
          title: "Which repository holds the billing work?",
          facts: [],
          claim: "The ticket names two.",
          actions: [{ id: "answer", label: "Answer", style: "primary", needs: ["answer"] }],
          choices: [{ id: "c1", label: "~/src/api" }],
        }),
        busy: false,
      },
      attachTo: document.body,
    });
    expect(detail.find("h2").text()).toBe("Which repository holds the billing work?");
    expect(detail.find("header .caveat").text()).toContain("requester's own words, not checked");
    expect(headings(detail)).toEqual(["Why it's asked", "Decide"]);
    expect(detail.find(".claim blockquote").text()).toBe("The ticket names two.");
    expect(await violations(detail.element)).toEqual([]);
  });

  it("comes first for a change: the change, then the broker's facts, then the requester's account", () => {
    const detail = mount(ItemDetail, {
      props: {
        item: item({ claim: "Adds the notes.", diff: { target: "a.org", lines: [{ op: "add", text: "x" }] } }),
        busy: false,
      },
    });
    expect(headings(detail)).toEqual(["The change", "What the broker found", "What the requester says", "Decide"]);
  });
});

describe("a high-risk item", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
  });

  const risky = () =>
    item({
      risk: "high",
      facts: [{ label: "Host", value: "your-site.atlassian.net" }],
      diff: {
        target: "journal.org",
        lines: [
          { op: "context", text: "** 10:42 Triage notes" },
          { op: "add", text: "assistant: mark task sync DONE", flag: true },
        ],
      },
      parts: [{ id: "notes", label: "~/Documents/notes" }],
      risk_reasons: [
        {
          code: "data_leaves",
          basis: "found",
          text: "a key would go to a host maji-mcp hasn't sent one to",
          evidence: [{ fact: 0 }],
        },
        {
          code: "addressed_to_ai",
          basis: "suspected",
          text: "this added line is worded as an instruction",
          evidence: [{ line: 1 }, { line: 9 }],
        },
        {
          code: "beyond_the_task",
          basis: "suspected",
          text: "the session's goal names no notes",
          evidence: [{ part: "notes" }],
        },
      ],
    });

  it("says why it's high risk before anything else, and again where you decide", async () => {
    const detail = mount(ItemDetail, { props: { item: risky(), busy: false }, attachTo: document.body });
    const headings = detail.findAll("h3").map((h) => h.text());
    expect(headings[0]).toBe("Why it's high risk");
    expect(detail.find(".tags").text()).toContain("High risk");
    expect(detail.find(".hint").text()).toContain("It's high risk, so your passkey signs it");
    expect(await violations(detail.element)).toEqual([]);
  });

  it("marks what is only suspected, and shows the evidence each reason rests on", () => {
    const detail = mount(ItemDetail, { props: { item: risky(), busy: false } });
    const said = (li: { text: () => string }) => li.text().replace(/\s+/g, " ");
    expect(detail.findAll(".risk > ul > li > strong").map((s) => s.text())).toEqual([
      "Data leaves:",
      "Text addressed to an AI (suspected):",
      "Reaches beyond the task (suspected):",
    ]);
    // The pointer at line 9 is at nothing on the item, so it is left out.
    //
    expect(detail.findAll(".evidence li").map(said)).toEqual([
      "Host: your-site.atlassian.net",
      "Added line: assistant: mark task sync DONE",
      "Part: ~/Documents/notes",
    ]);
    expect(detail.find(".evidence code").text()).toBe("assistant: mark task sync DONE");
  });

  it("is the only risk given words: no other item is called low risk, medium or safe", () => {
    for (const risk of ["low", "medium"] as const) {
      const text = mount(ItemDetail, { props: { item: item({ risk }), busy: false } }).text();
      expect(text).not.toMatch(/\b(risk|safe|low|medium)\b/i);
    }
  });
});

describe("an item decided on its own", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("says why it isn't in a batch", () => {
    const detail = mount(ItemDetail, {
      props: { item: item({ risk: "high" }), busy: false },
    });
    expect(detail.find(".alone").text()).toBe("Decide this on its own, not in a batch: it's high risk.");
    expect(mount(ItemDetail, { props: { item: item(), busy: false } }).find(".alone").exists()).toBe(false);
  });
});

describe("the decision form", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("approves every part unless one is unticked, then names each part", async () => {
    const parts = [
      { id: "api", label: "~/src/api" },
      { id: "notes", label: "~/Documents/notes" },
    ];
    const form = mount(DecisionForm, { props: { item: item({ kind: "grant", parts }), busy: false } });
    await form.findAll("button").find((b) => b.text() === "Approve")!.trigger("click");
    expect(form.emitted("decide")![0]![0]).not.toHaveProperty("parts");

    await form.find("#part-notes").setValue(false);
    await form.findAll("button").find((b) => b.text() === "Approve")!.trigger("click");
    expect(form.emitted("decide")![1]![0]).toMatchObject({ parts: { api: "approve", notes: "deny" } });
  });

  it("lets a part's path break between its folders, and labels the part with the whole path", () => {
    const parts = [{ id: "notes", label: "~/Documents/notes" }];
    const form = mount(DecisionForm, { props: { item: item({ kind: "grant", parts }), busy: false } });
    const label = form.get('label[for="part-notes"]');
    expect(label.text()).toBe("~/Documents/notes");
    expect(label.findAll("wbr")).toHaveLength(2);
    expect(afterSlashes("~/Documents/notes")).toEqual(["~/", "Documents/", "notes"]);
    expect(afterSlashes("Draft the guide")).toEqual(["Draft the guide"]);
  });

  it("says which session \"For this session\" means, once, and with each choice that offers it", async () => {
    const key = "3f9a61c2" + "0".repeat(56);
    const mounts = {
      kind: "grant" as const,
      parts: [
        { id: "api", label: "~/src/api" },
        { id: "web", label: "~/src/web" },
      ],
      patterns: ["api", "web"].map((id) => ({
        id: `p-${id}`,
        summary: `Mounts of ~/src/${id}`,
        access: "mount" as const,
        resource: `~/src/${id}`,
        match: {},
        parts: [id],
      })),
    };
    const form = mount(DecisionForm, {
      props: {
        item: item({ ...mounts, requester: { principal: "claude-code", session: key }, session_name: "laughing-sparky-daisy" }),
        busy: false,
      },
      attachTo: document.body,
    });
    const line = form.get(".this-session");
    expect(line.text().replace(/\s+/g, " ")).toBe("This session is laughing-sparky-daisy 3f9a61c2.");
    expect(line.get(".key").attributes("aria-hidden")).toBe("true");
    // The choice for every part, and each part's own.
    const selects = form.findAll("select");
    expect(selects).toHaveLength(3);
    expect(selects.map((s) => s.attributes("aria-describedby"))).toEqual(Array(3).fill(line.attributes("id")));
    expect(await violations(form.element)).toEqual([]);

    const sessionless = mount(DecisionForm, {
      props: { item: item({ ...mounts, requester: { principal: "claude-code" } }), busy: false },
    });
    expect(sessionless.find(".this-session").exists()).toBe(false);
    expect(sessionless.get("select").attributes("aria-describedby")).toBeUndefined();
    const nothingRemembered = mount(DecisionForm, {
      props: { item: item({ requester: { principal: "claude-code", session: key } }), busy: false },
    });
    expect(nothingRemembered.find(".this-session").exists()).toBe(false);
  });

  it("remembers an approval for the session when a duration is chosen", async () => {
    const form = mount(DecisionForm, {
      props: {
        item: item({ patterns: [{ id: "p", summary: "Ticks", access: "structural", match: {} }] }),
        busy: false,
      },
    });
    await form.findAll("button").find((b) => b.text() === "Approve")!.trigger("click");
    expect(form.emitted("decide")![0]![0]).not.toHaveProperty("remember");

    await form.find("#keep-p").setValue("session");
    await form.findAll("button").find((b) => b.text() === "Approve")!.trigger("click");
    expect(form.emitted("decide")![1]![0]).toMatchObject({ remember: [{ pattern: "p", lasts: "session" }] });
  });

  it("remembers each part on its own, and never a part you refuse", async () => {
    const repositories = { api: "~/src/api", web: "~/src/web", notes: "~/Documents/notes" };
    const form = mount(DecisionForm, {
      props: {
        item: item({
          kind: "grant",
          requester: { principal: "claude-code", session: "s-1", run: "r-1" },
          parts: Object.entries(repositories).map(([id, label]) => ({ id, label })),
          patterns: Object.entries(repositories).map(([id, path]) => ({
            id: `mount-${id}`,
            summary: `Read-only mounts of ${path}`,
            access: "mount",
            match: {},
            parts: [id],
          })),
        }),
        busy: false,
      },
      attachTo: document.body,
    });
    expect(form.find("label[for='keep-mount-api']").text()).toBe("Remember Read-only mounts of ~/src/api");

    await form.find("#keep-mount-api").setValue("permanent");
    await form.find("#keep-mount-notes").setValue("permanent");
    await form.find("#part-notes").setValue(false);
    expect((form.find("#keep-mount-notes").element as HTMLSelectElement).disabled).toBe(true);
    await form.findAll("button").find((b) => b.text() === "Approve")!.trigger("click");

    expect(form.emitted("decide")![0]![0]).toMatchObject({
      parts: { api: "approve", web: "approve", notes: "deny" },
      remember: [
        { pattern: "mount-api", lasts: "permanent" },
        { pattern: "mount-web", lasts: "session" },
      ],
    });
    expect(await violations(form.element)).toEqual([]);
  });

  it("sets how long every part is remembered with one choice, leaving each part its own", async () => {
    const repositories = { api: "~/src/api", web: "~/src/web", notes: "~/Documents/notes" };
    const form = mount(DecisionForm, {
      props: {
        item: item({
          id: "i-1",
          kind: "grant",
          requester: { principal: "claude-code", session: "s-1" },
          parts: Object.entries(repositories).map(([id, label]) => ({ id, label })),
          patterns: Object.entries(repositories).map(([id, path]) => ({
            id: `mount-${id}`,
            summary: `Read-only mounts of ${path}`,
            access: "mount",
            match: {},
            parts: [id],
          })),
        }),
        busy: false,
      },
      attachTo: document.body,
    });
    const every = () => form.find("#keep-every-i-1").element as HTMLSelectElement;
    const hint = () => form.find("#passkey-note-i-1").text();

    // It starts where every part starts, at the shortest a mount is kept for, and
    // offers what every part offers.
    expect(form.find("label[for='keep-every-i-1']").text()).toBe("Remember all");
    expect(every().value).toBe("session");
    expect(form.findAll("#keep-every-i-1 option").map((o) => o.text())).toEqual([
      "For this session",
      "For this session, 8 hours at most",
      "Until a time you choose",
      "Always, until you revoke it",
    ]);
    expect(hint()).not.toContain("your passkey signs it");

    // A part you refuse is left as it was, and the rest follow the one choice.
    await form.find("#part-notes").setValue(false);
    await form.find("#keep-every-i-1").setValue("permanent");
    expect((form.find("#keep-mount-api").element as HTMLSelectElement).value).toBe("permanent");
    expect((form.find("#keep-mount-web").element as HTMLSelectElement).value).toBe("permanent");
    expect((form.find("#keep-mount-notes").element as HTMLSelectElement).value).toBe("session");
    expect(hint()).toContain("your passkey signs it");

    // Changing one part alone is still that part's choice, and the one choice says so.
    await form.find("#keep-mount-web").setValue("session");
    expect(every().value).toBe("mixed");
    expect(form.find("#keep-every-i-1 option[value='mixed']").text()).toBe("Each as chosen below");

    await form.findAll("button").find((b) => b.text() === "Approve")!.trigger("click");
    expect(form.emitted("decide")![0]![0]).toMatchObject({
      parts: { api: "approve", web: "approve", notes: "deny" },
      remember: [
        { pattern: "mount-api", lasts: "permanent" },
        { pattern: "mount-web", lasts: "session" },
      ],
    });

    // One time is set for every part at once.
    await form.find("#keep-every-i-1").setValue("until");
    await form.find("#until-every-i-1").setValue("2099-01-02T09:00");
    expect((form.find("#until-mount-api").element as HTMLInputElement).value).toBe("2099-01-02T09:00");
    expect((form.find("#until-mount-web").element as HTMLInputElement).value).toBe("2099-01-02T09:00");
    expect(await violations(form.element)).toEqual([]);
  });

  it("offers no choice for every part when there is one part", () => {
    const form = mount(DecisionForm, {
      props: {
        item: item({
          id: "i-1",
          kind: "grant",
          requester: { principal: "claude-code", session: "s-1" },
          parts: [{ id: "api", label: "~/src/api" }],
          patterns: [{ id: "mount-api", summary: "Read-only mounts of ~/src/api", access: "mount", match: {}, parts: ["api"] }],
        }),
        busy: false,
      },
    });
    expect(form.find("#keep-every-i-1").exists()).toBe(false);
  });

  it("never offers a mount just once, and keeps it for the session unless you choose", async () => {
    const pattern = { id: "m", summary: "Read-only mounts of ~/src/api", access: "mount" as const, match: {} };
    const form = (requester: Record<string, string>) =>
      mount(DecisionForm, {
        props: { item: item({ kind: "grant", requester: { principal: "claude-code", ...requester }, patterns: [pattern] } as never), busy: false },
      });
    const offered = (requester: Record<string, string>) =>
      form(requester)
        .findAll("#keep-m option")
        .map((o) => (o.element as HTMLOptionElement).value);
    expect(offered({ session: "s-1", run: "r-1" })).toEqual(["session", "hours", "until", "permanent"]);
    expect(offered({})).toEqual(["hours", "until", "permanent"]);

    const inSession = form({ session: "s-1" });
    expect(inSession.text()).toContain("A mount is never approved just once");
    expect(inSession.text()).toContain("A session lasts until the agent's app closes or reconnects");
    await inSession.findAll("button").find((b) => b.text() === "Approve")!.trigger("click");
    expect(inSession.emitted("decide")![0]![0]).toMatchObject({ remember: [{ pattern: "m", lasts: "session" }] });

    // Kept for 8 hours, how long that lasts is measured from the click.
    //
    const keptFor = async (decided: ReturnType<typeof form>) => {
      const before = Date.now();
      await decided.findAll("button").find((b) => b.text() === "Approve")!.trigger("click");
      const [kept] = (decided.emitted("decide")![0]![0] as { remember: { lasts: string; until: string }[] }).remember;
      return { lasts: kept!.lasts, hours: (new Date(kept!.until).getTime() - before) / 3_600_000 };
    };

    const capped = form({ session: "s-1" });
    expect(capped.find("#keep-m option[value='hours']").text()).toBe("For this session, 8 hours at most");
    await capped.find("#keep-m").setValue("hours");
    expect(capped.text()).not.toContain("lasts beyond the run or session");
    const withinSession = await keptFor(capped);
    expect(withinSession.lasts).toBe("session");
    expect(withinSession.hours).toBeGreaterThan(7.99);
    expect(withinSession.hours).toBeLessThan(8.01);

    const noSession = form({});
    expect(noSession.text()).toContain("The agent's app named no session, so a mount is kept for a time instead.");
    expect(noSession.text()).not.toContain("A session lasts");
    expect(noSession.find("#keep-m option[value='hours']").text()).toBe("For 8 hours");
    expect(noSession.text()).toContain("lasts beyond the run or session");
    const anyRun = await keptFor(noSession);
    expect(anyRun.lasts).toBe("until");
    expect(anyRun.hours).toBeGreaterThan(7.99);
    expect(anyRun.hours).toBeLessThan(8.01);
  });

  it("offers only durations a grant can be held to, and a time only in the future", async () => {
    const pattern = { id: "p", summary: "Ticks", access: "structural" as const, match: {} };
    const offered = (requester: Record<string, string>) =>
      mount(DecisionForm, {
        props: { item: item({ requester: { principal: "claude-code", ...requester }, patterns: [pattern] } as never), busy: false },
      })
        .findAll("#keep-p option")
        .map((o) => (o.element as HTMLOptionElement).value);
    expect(offered({})).toEqual(["", "until", "permanent"]);
    expect(offered({ session: "s-1" })).toEqual(["", "session", "until", "permanent"]);
    expect(offered({ session: "s-1", run: "r-1" })).toEqual(["", "run", "session", "until", "permanent"]);

    const form = mount(DecisionForm, { props: { item: item({ patterns: [pattern] }), busy: false } });
    await form.find("#keep-p").setValue("until");
    await form.find("#until-p").setValue("2020-01-01T09:00");
    await form.findAll("button").find((b) => b.text() === "Approve")!.trigger("click");
    expect(form.emitted("decide")).toBeUndefined();
    expect(form.find("[role=alert]").text()).toContain("Choose a time in the future");
  });

  it("won't return an item without a note, or answer a question without an answer", async () => {
    const form = mount(DecisionForm, { props: { item: item(), busy: false } });
    await form.findAll("button").find((b) => b.text() === "Return with note")!.trigger("click");
    expect(form.emitted("decide")).toBeUndefined();
    expect(form.find("[role=alert]").text()).toContain("needs a note");

    const question = mount(DecisionForm, {
      props: {
        item: item({
          kind: "question",
          actions: [{ id: "answer", label: "Answer", style: "primary", needs: ["answer"] }],
          choices: [{ id: "c1", label: "~/src/api" }],
        }),
        busy: false,
      },
    });
    await question.find("button").trigger("click");
    expect(question.emitted("decide")).toBeUndefined();
    await question.find("#answer-c1").setValue(true);
    await question.find("button").trigger("click");
    expect(question.emitted("decide")![0]![0]).toMatchObject({ answer: { choice: "c1" } });
  });

  const tracker = {
    service: "tracker",
    label: "Tracker",
    method: "api_key" as const,
    host: "tracker.example",
    sent_as: "Authorization: Bearer",
    permissions: ["issues:read"],
    key_page: "https://tracker.example/keys",
    settings: [{ name: "org", label: "Organization slug", pattern: "[a-z]{1,20}" }],
  };
  const connect = () =>
    item({
      kind: "connect",
      title: "Connect Tracker",
      connection: tracker,
      actions: [
        { id: "connect", label: "Connect", style: "primary", needs: ["credential"] },
        { id: "deny", label: "Deny", style: "danger" },
      ],
    });

  it("connects with the key beside the decision, which names only its fingerprint", async () => {
    const form = mount(DecisionForm, { props: { item: connect(), busy: false } });
    const key = "tok_abc123";

    await form.find("input[type=password]").setValue(` ${key} `);
    await form.find(`#setting-${connect().id}-org`).setValue("acme");
    await form.findAll("button").find((b) => b.text() === "Connect")!.trigger("click");

    // The form emits once WebCrypto has hashed the key, which Node does on its
    // thread pool, so the event can come after any number of ticks.
    //
    await vi.waitFor(() => expect(form.emitted("decide")).toBeDefined());

    const [choice, pasted] = form.emitted("decide")![0]! as [Record<string, unknown>, Record<string, string>];
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(key));
    const hex = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
    expect(choice).toMatchObject({
      action: "connect",
      credential_fingerprint: `sha256:${hex}`,
      credential_settings: { org: "acme" },
    });
    expect(JSON.stringify(choice)).not.toContain(key);
    expect(pasted).toEqual({ [connect().id]: key });
    expect((form.find("input[type=password]").element as HTMLInputElement).value).toBe("");
  });

  it("offers Connect only on an item that names its service", () => {
    const named = mount(DecisionForm, { props: { item: connect(), busy: false } });
    const unnamed = mount(DecisionForm, { props: { item: { ...connect(), connection: undefined }, busy: false } });

    expect(named.findAll(".actions button").map((b) => b.text())).toEqual(["Connect", "Deny"]);
    expect(unnamed.findAll(".actions button").map((b) => b.text())).toEqual(["Deny"]);
  });

  it("asks for the key and its settings before connecting, and says where the key goes", async () => {
    const form = mount(DecisionForm, { props: { item: connect(), busy: false } });
    const button = form.findAll("button").find((b) => b.text() === "Connect")!;

    await button.trigger("click");
    await new Promise((settled) => setTimeout(settled, 0));
    expect(form.find("[role=alert]").text()).toBe("Paste the Tracker key first.");

    await form.find("input[type=password]").setValue("tok_abc123");
    await form.find(`#setting-${connect().id}-org`).setValue("Not A Slug");
    await button.trigger("click");
    await new Promise((settled) => setTimeout(settled, 0));
    expect(form.find("[role=alert]").text()).toBe("Organization slug doesn't look like one.");
    expect(form.emitted("decide")).toBeUndefined();

    const said = form.find("fieldset").text().replace(/\s+/g, " ");
    expect(said).toContain("only this: issues:read");
    expect(said).toContain("sends it only to tracker.example");
    expect(form.find("a").attributes("href")).toBe("https://tracker.example/keys");
    expect(form.find("input[type=password]").attributes("autocomplete")).toBe("off");
  });
});

describe("a decided item", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
  });

  const repositories = { api: "~/src/api", web: "~/src/web", notes: "~/Documents/notes" };
  const mounts = {
    kind: "grant",
    state: "decided",
    title: "Mount three repositories for this run",
    parts: Object.entries(repositories).map(([id, label]) => ({ id, label })),
    patterns: Object.entries(repositories).map(([id, path]) => ({
      id: `mount-${id}`,
      summary: `Read-only mounts of ${path}`,
      access: "mount",
      match: {},
      parts: [id],
    })),
  } as const;

  function decision(overrides: Record<string, unknown>) {
    return {
      id: "d",
      item: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b8n",
      content_hash: `sha256:${"a".repeat(64)}`,
      action: "approve",
      decision_hash: `sha256:${"c".repeat(64)}`,
      by: { kind: "you" },
      at: "2026-11-08T12:00:00Z",
      ...overrides,
    };
  }

  function parts(detail: ReturnType<typeof mount>): string[] {
    return detail.findAll("ul.parts li").map((li) => li.text().replace(/\s+/g, " "));
  }

  it("says what became of each part, and how long each is remembered", async () => {
    const detail = mount(ItemDetail, {
      props: {
        item: item({
          ...mounts,
          decision: decision({
            parts: { api: "approve", web: "approve", notes: "deny" },
            remember: [{ pattern: "mount-api", lasts: "run" }],
            granted: ["g1"],
          }),
        } as never),
        busy: false,
      },
      attachTo: document.body,
    });
    expect(parts(detail)).toEqual([
      "~/src/api: approved, remembered for the rest of this run.",
      "~/src/web: approved.",
      "~/Documents/notes: refused.",
    ]);
    expect(await violations(detail.element)).toEqual([]);
  });

  it("names the rule that decided it, and what was remembered over the whole request", () => {
    const detail = mount(ItemDetail, {
      props: {
        item: item({
          state: "decided",
          parts: [{ id: "guide", label: "Draft the onboarding guide" }],
          patterns: [{ id: "ticks", summary: "Ticks in task-demo's checklist", access: "structural", match: {} }],
          decision: decision({
            by: { kind: "policy", rule: "structural changes you asked for" },
            remember: [{ pattern: "ticks", lasts: "permanent" }],
          }),
        } as never),
        busy: false,
      },
    });
    const record = detail.find(".record").text().replace(/\s+/g, " ");
    expect(record).toContain("Approved by the rule “structural changes you asked for”");
    expect(parts(detail)).toEqual(["Draft the onboarding guide: approved."]);
    expect(record).toContain("Remembered: Ticks in task-demo's checklist, until you revoke it.");
  });

  const promotion = {
    kind: "promote",
    state: "decided",
    risk: "high",
    title: "Promote skills.tick_item",
    subject: "skills.tick_item · me/local/tick_item",
    skill: { id: "me/local/tick_item", version: "66f3f0a93b493aa7" },
  } as const;

  it("offers to demote a promotion in effect, taking back the promotion by its id", async () => {
    const promoted = item({
      ...promotion,
      actions: [{ id: "demote", label: "Demote", style: "danger" }],
      decision: decision({ id: "d-promote", action: "promote", signature: { passkey: "k" } }),
    } as never);
    const detail = mount(ItemDetail, { props: { item: promoted, busy: false }, attachTo: document.body });

    const record = detail.find(".record");
    expect(record.text()).toContain("Promoted by you");
    const section = detail.find(".reconsider");
    expect(section.find("h4").text()).toBe("Demote");
    expect(section.text()).toContain("Demoting stops every agent running this version, the agent that kept it too.");
    expect(section.text()).not.toContain("high risk");
    expect(section.find("label").text()).toBe("Why (optional): agents that try it see this");
    expect(await violations(detail.element)).toEqual([]);

    await section.find("textarea").setValue("It ticks the wrong item.");
    await section.findAll("button").find((b) => b.text() === "Demote")!.trigger("click");
    expect(detail.emitted("decide")).toEqual([
      [
        {
          item: promoted.id,
          content_hash: promoted.content_hash,
          action: "demote",
          supersedes: "d-promote",
          note: "It ticks the wrong item.",
        },
      ],
    ]);
  });

  it("says what a demotion took back, and offers nothing more", () => {
    const demoted = item({
      ...promotion,
      actions: [],
      decision: decision({ id: "d-demote", action: "demote", supersedes: "d-promote", note: "Wrong item." }),
      superseded: decision({ id: "d-promote", action: "promote", at: "2026-11-08T09:00:00Z" }),
    } as never);
    const detail = mount(ItemDetail, { props: { item: demoted, busy: false } });
    const text = detail.find(".record").text().replace(/\s+/g, " ");
    expect(text).toContain("Demoted by you");
    expect(text).toContain("It takes back your earlier decision: Promoted,");
    expect(text).toContain("Your note: Wrong item.");
    expect(detail.find(".reconsider").exists()).toBe(false);
  });

  it("lets the agent ask again for a mount you refused, then says when you did", async () => {
    const refused = item({
      ...mounts,
      decision: decision({
        parts: { api: "approve", web: "approve", notes: "deny" },
        remember: [
          { pattern: "mount-api", lasts: "session" },
          { pattern: "mount-web", lasts: "session" },
        ],
        granted: ["g1", "g2"],
      }),
    } as never);
    const detail = mount(ItemDetail, { props: { item: refused, busy: false }, attachTo: document.body });
    const button = detail.findAll("button").find((b) => b.text() === "Let it ask again");
    expect(button).toBeDefined();
    expect(await violations(detail.element)).toEqual([]);
    await button!.trigger("click");
    expect(detail.emitted("askAgain")).toEqual([[refused.id]]);

    await detail.setProps({ item: { ...refused, ask_again: { at: "2026-11-08T13:00:00Z", by: "passkey:k" } } });
    expect(detail.findAll("button").some((b) => b.text() === "Let it ask again")).toBe(false);
    expect(detail.find(".record").text()).toContain("You let the agent ask again for what you refused");
  });

  it("offers to let it ask again only where a mount was refused", () => {
    const shown = (decided: Partial<Item>) =>
      mount(ItemDetail, { props: { item: item(decided as never), busy: false } })
        .findAll("button")
        .some((b) => b.text() === "Let it ask again");
    const everyMount = ["api", "web", "notes"].map((id) => ({ pattern: `mount-${id}`, lasts: "session" }));
    expect(shown({ ...mounts, decision: decision({ action: "deny" }) } as never)).toBe(true);
    expect(shown({ ...mounts, decision: decision({ remember: everyMount }) } as never)).toBe(false);
    expect(shown({ state: "decided", decision: decision({ action: "reject" }) } as never)).toBe(false);
  });

  it("lists what a refused request asked for without marking each part", () => {
    const detail = mount(ItemDetail, {
      props: { item: item({ ...mounts, decision: decision({ action: "deny" }) } as never), busy: false },
    });
    expect(parts(detail)).toEqual(["~/src/api.", "~/src/web.", "~/Documents/notes."]);
  });

  it("shows the answer a question was given", () => {
    const detail = mount(ItemDetail, {
      props: {
        item: item({
          kind: "question",
          state: "decided",
          choices: [
            { id: "c1", label: "~/src/api" },
            { id: "c2", label: "~/src/billing" },
          ],
          decision: decision({ action: "answer", answer: { choice: "c2" } }),
        } as never),
        busy: false,
      },
    });
    expect(detail.find(".record").text()).toContain("Your answer: ~/src/billing");
  });
});

describe("an item open in its row", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
  });

  it("is headed a level lower, under its list, and leaves its title, subject, tags and session to the row", async () => {
    // Under a list's heading, as it is on the page.
    //
    const list = document.createElement("section");
    list.innerHTML = "<h2>Waiting</h2>";
    document.body.append(list);
    const detail = mount(ItemDetail, {
      props: {
        item: item({
          subject: "journal/2026-11-08.org",
          claim: "Fixes a typo.",
          requester: { principal: "claude-code", session: "3f9a61c2" + "0".repeat(56) },
          session_name: "laughing-sparky-daisy",
        }),
        busy: false,
        inline: true,
      },
      attachTo: list,
    });
    const title = detail.get("[data-item-heading]");
    expect(title.element.tagName).toBe("H3");
    expect(title.text()).toBe("Update journal entry");
    expect(title.classes()).toContain("visually-hidden");
    expect(title.attributes("tabindex")).toBe("-1");
    expect(detail.find(".subject").exists()).toBe(false);
    expect(detail.find(".tags").exists()).toBe(false);
    expect(detail.find(".from").text()).toContain("From claude-code");

    // The row above shows the session, so the item says it only to screen readers,
    // key and all.
    //
    const session = detail.get(".from .session").element.closest(".from > span")!;
    expect(session.classList).toContain("visually-hidden");
    expect(session.textContent!.replace(/\s+/g, " ").trim()).toBe("in session laughing-sparky-daisy 3f9a61c2");
    expect(session.querySelector("[aria-hidden]")).toBeNull();
    expect(detail.findAll("h4").map((h) => h.text())).toEqual([
      "What the broker found",
      "What the requester says",
      "Decide",
    ]);
    expect(detail.findAll("h2, h3").map((h) => h.text())).toEqual(["Update journal entry"]);
    expect(await violations(list)).toEqual([]);
  });
});
