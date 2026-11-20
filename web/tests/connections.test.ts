/**
 * The services scripts can call with a key you paste: each one's state, where its
 * key goes, and the way to connect it, replace its key or disconnect it, which all
 * lead to a Connect item in the queue. The control plane's answers are stubbed.
 */
import { enableAutoUnmount, flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMemoryHistory, createRouter } from "vue-router";

import type { Connection } from "@/api/client";

import { violations } from "./axe";

enableAutoUnmount(afterEach);

// openapi-fetch takes fetch when the client is made, at import, so the stub has to be
// in place before any module loads; vi.hoisted runs first. The connections answer
// from whatever the test sets, a request to connect answers with an item, and each
// request's method and path are kept.
//
const stub = vi.hoisted(() => {
  const state = { connections: [] as unknown[], asked: [] as string[] };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      state.asked.push(`${request.method} ${url.pathname}`);
      const body = url.pathname.endsWith("/requests")
        ? { id: "i-new" }
        : url.pathname.endsWith("/items")
          ? { items: [] }
          : { connections: state.connections };
      return new Response(JSON.stringify(body), {
        status: request.method === "POST" ? 201 : 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return state;
});

const ConnectionsView = (await import("@/views/ConnectionsView.vue")).default;
const { useQueue } = await import("@/stores/queue");

const sentry: Connection = {
  service: "sentry",
  label: "Sentry",
  method: "api_key",
  host: "sentry.io",
  status: "connected",
  fingerprint: "sha256:" + "ab".repeat(32),
  settings: { org: "acme" },
  connected_at: "2027-01-15T08:00:00Z",
  decision: "d-1",
  item: "i-sentry",
};
const tracker: Connection = {
  service: "tracker",
  label: "Tracker",
  method: "api_key",
  host: "tracker.example",
  status: "not_connected",
};

async function shown() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/connections", name: "connections", component: ConnectionsView },
      { path: "/queue", name: "queue", component: { template: "<p>queue</p>" } },
    ],
  });
  await router.push("/connections");
  const page = mount(ConnectionsView, { global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return { page, router };
}

describe("the connections", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
    stub.asked.length = 0;
    stub.connections = [sentry, tracker];
  });

  it("says of each service whether it's connected, where its key goes, and its key's fingerprint", async () => {
    const { page } = await shown();

    const [first, second] = page.findAll("li").map((li) => ({
      name: li.find(".name").text(),
      meta: li.find(".meta").text().replace(/\s+/g, " "),
    }));
    expect(first?.name).toBe("Sentry");
    expect(first?.meta).toContain("Connected · key sent only to sentry.io");
    expect(first?.meta).toContain(`key sha256:${"ab".repeat(4)}`);
    expect(first?.meta).toContain("org acme");
    expect(second?.name).toBe("Tracker");
    expect(second?.meta).toContain("Not connected · key sent only to tracker.example");
    expect(await violations(page.element)).toEqual([]);
  });

  it("asks to connect a service in the queue, and opens the Connect item there", async () => {
    const { page, router } = await shown();

    await page.get("[aria-label='Connect Tracker']").trigger("click");
    await flushPromises();

    expect(stub.asked).toContain("POST /api/v0/connections/tracker/requests");
    expect(useQueue().currentId).toBe("i-new");
    expect(router.currentRoute.value.name).toBe("queue");
  });

  it("offers a connected service a new key, and its connecting item to disconnect from", async () => {
    const { page, router } = await shown();

    expect(page.find("[aria-label='Replace the Sentry key']").exists()).toBe(true);
    await page.get("[aria-label='Open the item that connected Sentry, to disconnect it']").trigger("click");
    await flushPromises();

    expect(useQueue().currentId).toBe("i-sentry");
    expect(router.currentRoute.value.name).toBe("queue");
  });
});
