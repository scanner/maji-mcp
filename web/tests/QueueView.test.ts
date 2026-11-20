/**
 * The queue page in a narrow window, where the item in front of you opens in its row,
 * and in a wide one, where it is shown beside the lists. The window's width is
 * stubbed, and the queue is filled directly rather than loaded.
 */
import { enableAutoUnmount, flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import QueueView from "@/views/QueueView.vue";
import { useQueue } from "@/stores/queue";

import { item } from "./fixtures";

enableAutoUnmount(afterEach);

// A window whose width can change while the page is open: `resize` tells every
// listener on the width query, as the browser would.
//
const width = vi.hoisted(() => {
  const listeners = new Set<(event: { matches: boolean }) => void>();
  const state = {
    narrow: true,
    resize(narrow: boolean) {
      state.narrow = narrow;
      for (const listener of listeners) listener({ matches: narrow });
    },
    listeners,
  };
  return state;
});
vi.stubGlobal("matchMedia", (query: string) => ({
  get matches() {
    return query.includes("max-width") && width.narrow;
  },
  addEventListener: (_: string, listener: (event: { matches: boolean }) => void) =>
    query.includes("max-width") && width.listeners.add(listener),
  removeEventListener: (_: string, listener: (event: { matches: boolean }) => void) =>
    width.listeners.delete(listener),
}));

const first = item({ id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b81", title: "First" });
const second = item({ id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b82", title: "Second" });
const done = item({
  id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b83",
  title: "Done",
  state: "decided",
  decision: {
    action: "approve",
    at: "2026-11-08T09:05:00+00:00",
    by: { kind: "you" },
  } as never,
});

async function render() {
  const queue = useQueue();
  queue.load = vi.fn(async () => {
    queue.ahead = [first, second];
    queue.behind = [done];
    queue.currentId ??= first.id;
    queue.loaded = true;
  });
  queue.listen = vi.fn();
  queue.stop = vi.fn();
  const page = mount(QueueView, { attachTo: document.body });
  await flushPromises();
  return { page, queue };
}

/** The title of the item open in each list's rows, or shown beside them. */
function shown(page: Awaited<ReturnType<typeof render>>["page"]) {
  const title = (selector: string) => {
    const heading = page.find(`${selector} [data-item-heading]`);
    return heading.exists() ? heading.text() : null;
  };
  return {
    waiting: title('[aria-label="Waiting for a decision"]'),
    decided: title('[aria-label="Decided, newest first"]'),
    beside: title(".now"),
  };
}

describe("the queue in a narrow window", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
    width.resize(true);
  });

  it("opens the item in front of you in its row, with nothing beside the lists", async () => {
    const { page } = await render();
    expect(shown(page)).toEqual({ waiting: "First", decided: null, beside: null });
    expect(page.find(".now").exists()).toBe(false);
  });

  it("opens the item you choose in its row, in either list, and closes it when chosen again", async () => {
    const { page } = await render();
    const row = (title: string) => page.findAll("li > button.open").find((b) => b.text().includes(title))!;

    await row("Second").trigger("click");
    expect(shown(page)).toEqual({ waiting: "Second", decided: null, beside: null });
    await row("Done").trigger("click");
    expect(shown(page)).toEqual({ waiting: null, decided: "Done", beside: null });
    await row("Done").trigger("click");
    expect(shown(page)).toEqual({ waiting: null, decided: null, beside: null });
    expect(row("Done").attributes("aria-expanded")).toBe("false");
    await row("Done").trigger("click");
    expect(shown(page).decided).toBe("Done");
  });

  it("opens the next item put in front of you, even after you closed the last", async () => {
    const { page, queue } = await render();
    await page.get("button.close").trigger("click");
    expect(shown(page).waiting).toBeNull();

    // A decision puts the next waiting item in front of you.
    //
    queue.currentId = second.id;
    await flushPromises();
    expect(shown(page).waiting).toBe("Second");
  });

  it("moves the open item beside the lists when the window widens, and back when it narrows", async () => {
    const { page } = await render();
    width.resize(false);
    await flushPromises();
    expect(shown(page)).toEqual({ waiting: null, decided: null, beside: "First" });
    expect(page.find("li > button.open").attributes("aria-expanded")).toBeUndefined();

    width.resize(true);
    await flushPromises();
    expect(shown(page)).toEqual({ waiting: "First", decided: null, beside: null });
  });
});

describe("the queue in a wide window", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
    width.resize(false);
  });

  it("shows the item you choose beside the lists, and keeps it there when chosen again", async () => {
    const { page } = await render();
    expect(shown(page)).toEqual({ waiting: null, decided: null, beside: "First" });
    const second = page.findAll("li > button.open")[1]!;
    await second.trigger("click");
    await second.trigger("click");
    expect(shown(page)).toEqual({ waiting: null, decided: null, beside: "Second" });
  });
});
