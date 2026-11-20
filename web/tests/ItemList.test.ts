/**
 * The waiting list: one stop in the Tab order, moved through with the arrows and
 * Emacs's keys, single-key shortcuts that can be turned off, and selection for a batch.
 */
import { enableAutoUnmount, flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ItemList from "@/components/ItemList.vue";
import { WAITING_TAG, ago, shortWhen } from "@/lib/describe";
import { useAnnouncer } from "@/stores/announcer";
import { usePreferences } from "@/stores/preferences";
import { aloneBecause, batchable } from "@/stores/queue";

import { violations } from "./axe";
import { item } from "./fixtures";

// Each list shares one clock, which ticks only while a list is mounted.
//
enableAutoUnmount(afterEach);

const items = [
  item({ id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b81", title: "First" }),
  item({ id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b82", title: "Second" }),
  item({ id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b83", title: "Third" }),
];

function render() {
  return mount(ItemList, {
    props: { items, label: "Waiting", currentId: null, selected: new Set<string>(), selectable: true },
    attachTo: document.body,
  });
}

describe("the waiting list", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
  });

  it("moves focus with the arrow keys, wrapping at the ends", async () => {
    const list = render();
    const buttons = list.findAll("button.open");
    (buttons[0]!.element as HTMLElement).focus();
    await buttons[0]!.trigger("keydown", { key: "ArrowDown" });
    expect(document.activeElement).toBe(buttons[1]!.element);
    await buttons[1]!.trigger("keydown", { key: "End" });
    expect(document.activeElement).toBe(buttons[2]!.element);
    await buttons[2]!.trigger("keydown", { key: "ArrowDown" });
    expect(document.activeElement).toBe(buttons[0]!.element);
  });

  it("moves with C-n and C-p, and to the ends with M-< and M->", async () => {
    const list = render();
    const buttons = list.findAll("button.open");
    (buttons[0]!.element as HTMLElement).focus();
    await buttons[0]!.trigger("keydown", { key: "n", ctrlKey: true });
    expect(document.activeElement).toBe(buttons[1]!.element);
    await buttons[1]!.trigger("keydown", { key: "p", ctrlKey: true });
    expect(document.activeElement).toBe(buttons[0]!.element);
    // macOS's Option turns < and > into other characters, so only the key's code is
    // the same as on other systems.
    //
    await buttons[0]!.trigger("keydown", { key: "¯", code: "Period", altKey: true, shiftKey: true });
    expect(document.activeElement).toBe(buttons[2]!.element);
    await buttons[2]!.trigger("keydown", { key: "˘", code: "Comma", altKey: true, shiftKey: true });
    expect(document.activeElement).toBe(buttons[0]!.element);
  });

  it("is one stop in the Tab order, at the row last focused", async () => {
    const list = render();
    const rows = list.findAll("li").map((w) => w.element);
    const tabbable = () =>
      new Set(list.findAll("[tabindex='0']").map((w) => rows.indexOf(w.element.closest("li")!)));
    expect(tabbable()).toEqual(new Set([0]));

    const buttons = list.findAll("button.open");
    (buttons[0]!.element as HTMLElement).focus();
    await buttons[0]!.trigger("keydown", { key: "ArrowDown" });
    await buttons[1]!.trigger("focusin");
    expect(list.findAll("[tabindex='0']")).toHaveLength(2);
    expect(tabbable()).toEqual(new Set([1]));
  });

  it("keeps to the selection boxes when moving from one", async () => {
    const list = render();
    const boxes = list.findAll("input.pick");
    (boxes[0]!.element as HTMLElement).focus();
    await boxes[0]!.trigger("keydown", { key: "n", ctrlKey: true });
    expect(document.activeElement).toBe(boxes[1]!.element);
    await boxes[1]!.trigger("keydown", { key: "Enter" });
    expect(list.emitted("open")).toEqual([[items[1]!.id]]);
  });

  it("honours j, k and x only while single-key shortcuts are on", async () => {
    const list = render();
    const buttons = list.findAll("button.open");
    await buttons[0]!.trigger("keydown", { key: "j" });
    expect(document.activeElement).toBe(buttons[1]!.element);
    await buttons[1]!.trigger("keydown", { key: "x" });
    expect(list.emitted("toggle")).toEqual([[items[1]!.id]]);

    usePreferences().shortcuts = false;
    (buttons[0]!.element as HTMLElement).focus();
    await buttons[0]!.trigger("keydown", { key: "j" });
    await buttons[0]!.trigger("keydown", { key: "x" });
    await buttons[0]!.trigger("keydown", { key: "x", ctrlKey: true });
    expect(document.activeElement).toBe(buttons[0]!.element);
    expect(list.emitted("toggle")).toHaveLength(1);
  });

  it("labels each selection box with the item it selects, and passes axe", async () => {
    const list = render();
    const label = list.find(`label[for="select-${items[0]!.id}"]`);
    expect(label.text()).toContain("First");
    expect(await violations(list.element.parentElement!)).toEqual([]);
  });

  it("keeps items with parts, a remember choice or high risk out of batches, and says why", async () => {
    usePreferences().shortcuts = true;
    const plain = item({ id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b84", title: "Plain" });
    const mounts = item({
      id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b85",
      title: "Mounts",
      parts: [{ id: "api", label: "~/src/api" }],
    });
    const tick = item({
      id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b86",
      title: "Tick",
      patterns: [{ id: "p", summary: "Ticks", access: "structural", match: {} }],
    });
    const risky = item({ id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b87", title: "Risky", risk: "high" });
    const question = item({
      id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b88",
      title: "Question",
      kind: "question",
      actions: [{ id: "answer", label: "Answer", style: "primary", needs: ["answer"] }],
    });
    expect([plain, mounts, tick, risky, question].map(batchable)).toEqual([true, false, false, false, false]);
    expect(aloneBecause(question)).toBeNull();

    const list = mount(ItemList, {
      props: {
        items: [plain, mounts, tick, risky, question],
        label: "Waiting",
        currentId: null,
        selected: new Set<string>(),
        selectable: true,
      },
      attachTo: document.body,
    });
    const rows = list.findAll("li");
    expect(rows.map((r) => r.find("input.pick").exists())).toEqual([true, false, false, false, false]);
    expect(rows.map((r) => r.text().includes("Decide on its own"))).toEqual([false, true, true, true, false]);

    vi.useFakeTimers();
    await rows[1]!.find("button.open").trigger("keydown", { key: "x" });
    vi.runAllTimers();
    vi.useRealTimers();
    expect(list.emitted("toggle")).toBeUndefined();
    expect(useAnnouncer().status).toContain(
      "Mounts is decided on its own: it asks for several things, and you may want to refuse some.",
    );
    expect(await violations(list.element)).toEqual([]);
  });
});

describe("each row of a list", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
  });

  const asked = "2026-11-08T09:00:00+00:00";
  const decidedAt = "2026-11-09T17:30:00+00:00";
  const rows = () =>
    mount(ItemList, {
      props: {
        items: [
          item({ id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b84", title: "Waiting", blocking: true, created_at: asked }),
          item({
            id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b85",
            title: "Done",
            state: "decided",
            blocking: true,
            created_at: asked,
            decision: { action: "deny", at: decidedAt } as never,
          }),
        ],
        label: "Items",
        currentId: null,
      },
    }).findAll("li");

  it("says when its item was asked, or once decided, when it was decided", () => {
    const [waiting, done] = rows();
    expect(waiting!.find(".when").text()).toBe(`Asked ${shortWhen(asked)}, ${ago(asked)}`);
    expect(waiting!.find("time").attributes("datetime")).toBe(asked);
    expect(done!.find(".when").text()).toBe(`Decided ${shortWhen(decidedAt)}, ${ago(decidedAt)}`);
    expect(done!.find("time").attributes("datetime")).toBe(decidedAt);
  });

  it("says how long ago as the time passes, out of a screen reader's way", async () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-11-10T16:00:05Z"));
      const justAsked = "2026-11-10T16:00:00Z";
      const list = mount(ItemList, {
        props: { items: [item({ created_at: justAsked })], label: "Items", currentId: null },
      });
      const said = () => list.find(".when [aria-hidden='true']").text();
      expect(said()).toBe(`, ${ago(justAsked, new Date("2026-11-10T16:00:05Z"))}`);
      await vi.advanceTimersByTimeAsync(60_000);
      expect(said()).toBe(`, ${ago(justAsked, new Date("2026-11-10T16:01:05Z"))}`);
    } finally {
      vi.useRealTimers();
    }
  });

  it("counts seconds under a minute, then minutes, hours and days", () => {
    const at = "2026-11-10T12:00:00Z";
    const after = (seconds: number) => ago(at, new Date(Date.parse(at) + seconds * 1000));
    const words = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
    expect(after(5)).toBe(words.format(-5, "second"));
    expect(after(59)).toBe(words.format(-59, "second"));
    expect(after(60)).toBe(words.format(-1, "minute"));
    expect(after(3 * 3600 + 59)).toBe(words.format(-3, "hour"));
    expect(after(2 * 86400)).toBe(words.format(-2, "day"));
    expect(after(-30)).toBe(words.format(0, "second"));
  });

  it("names the session that asked, leaving the start of its key out of what is read", () => {
    const key = "3f9a61c2" + "0".repeat(56);
    const list = mount(ItemList, {
      props: {
        items: [
          item({ requester: { principal: "claude-code", session: key }, session_name: "laughing-sparky-daisy" }),
          item({ id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b8x", requester: { principal: "claude-code" } }),
        ],
        label: "Waiting",
        currentId: null,
      },
    });
    const [named, none] = list.findAll("li > button.open");
    const meta = named!.find(".meta");
    expect(meta.find(".name").text()).toBe("laughing-sparky-daisy");
    expect(meta.find(".key").text()).toBe("3f9a61c2");
    expect(meta.find(".key").attributes("aria-hidden")).toBe("true");
    expect(meta.find(".visually-hidden").text()).toBe("in session");
    expect(none!.find(".session").exists()).toBe(false);
  });

  it("says an agent is waiting only while the item waits", () => {
    const [waiting, done] = rows();
    expect(waiting!.text()).toContain(WAITING_TAG);
    expect(waiting!.classes()).toContain("blocking");
    expect(done!.text()).not.toContain(WAITING_TAG);
    expect(done!.classes()).not.toContain("blocking");
  });

  it("gives the time alone today, the day too on another day, and the year in another year", () => {
    const now = new Date(2026, 10, 11, 16, 0);
    const today = new Date(2026, 10, 11, 9, 5).toISOString();
    const time = new Date(today).toLocaleTimeString(undefined, { timeStyle: "short" });
    expect(shortWhen(today, now)).toBe(time);
    const yesterday = shortWhen(new Date(2026, 10, 10, 9, 5).toISOString(), now);
    expect(yesterday).not.toBe(time);
    expect(yesterday).toContain("10");
    expect(yesterday).not.toContain("2026");
    expect(shortWhen(new Date(2025, 10, 10, 9, 5).toISOString(), now)).toContain("2025");
  });
});

describe("a list that opens items in place", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
    usePreferences().shortcuts = true;
  });

  // The page puts the item's detail in the slot; a field stands in for its controls.
  //
  function inPlace(currentId: string | null) {
    return mount(ItemList, {
      props: { items, label: "Waiting", currentId, selected: new Set<string>(), selectable: true, inline: true },
      slots: { detail: `<template #detail="{ item }"><input class="field" :aria-label="'Note on ' + item.title" /></template>` },
      attachTo: document.body,
    });
  }

  it("says which row is open, and puts the item's detail in that row alone", async () => {
    const list = inPlace(items[1]!.id);
    const buttons = list.findAll("li > button.open");
    expect(buttons.map((b) => b.attributes("aria-expanded"))).toEqual(["false", "true", "false"]);
    expect(buttons.map((b) => b.attributes("aria-current"))).toEqual([undefined, undefined, undefined]);
    expect(buttons[0]!.attributes("aria-controls")).toBeUndefined();
    const panel = document.getElementById(buttons[1]!.attributes("aria-controls")!)!;
    expect(panel.closest("li")).toBe(buttons[1]!.element.parentElement);
    expect(list.findAll(".field").map((f) => f.attributes("aria-label"))).toEqual(["Note on Second"]);
    expect(await violations(list.element.parentElement!)).toEqual([]);

    await list.setProps({ currentId: null });
    expect(buttons.map((b) => b.attributes("aria-expanded"))).toEqual(["false", "false", "false"]);
    expect(list.find(".field").exists()).toBe(false);
  });

  it("marks the item in front of you as current, not open, in a list that doesn't open in place", () => {
    const list = mount(ItemList, {
      props: { items, label: "Waiting", currentId: items[1]!.id },
      slots: { detail: `<input class="field" />` },
    });
    const buttons = list.findAll("li > button.open");
    expect(buttons.map((b) => b.attributes("aria-current"))).toEqual([undefined, "true", undefined]);
    expect(buttons[1]!.attributes("aria-expanded")).toBeUndefined();
    expect(list.find(".field").exists()).toBe(false);
  });

  it("leaves the keys pressed in an open item's fields to them", async () => {
    const list = inPlace(items[0]!.id);
    const field = list.find(".field");
    (field.element as HTMLElement).focus();
    for (const key of ["ArrowDown", "End", "j", "x"]) {
      const pressed = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
      field.element.dispatchEvent(pressed);
      expect(pressed.defaultPrevented).toBe(false);
    }
    expect(document.activeElement).toBe(field.element);
    expect(list.emitted("toggle")).toBeUndefined();

    // The row's own button still moves through the list.
    //
    const buttons = list.findAll("li > button.open");
    (buttons[0]!.element as HTMLElement).focus();
    await buttons[0]!.trigger("keydown", { key: "ArrowDown" });
    expect(document.activeElement).toBe(buttons[1]!.element);
  });

  it("closes the open item from its end, with focus back on its row", async () => {
    const list = inPlace(items[2]!.id);
    const close = list.get("button.close");
    expect(close.text()).toBe("Close");
    expect(close.attributes("aria-label")).toBe("Close: Third");
    await close.trigger("click");
    await flushPromises();
    expect(list.emitted("close")).toEqual([[items[2]!.id]]);
    expect(document.activeElement).toBe(list.findAll("li > button.open")[2]!.element);
  });
});
