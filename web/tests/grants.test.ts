/**
 * Grants grouped by the session that holds them: each session named as it is
 * everywhere, the grants held for no session picked out as their own group, and a
 * group revoked at once only where the control plane can find it by a filter. The
 * control plane's answers are stubbed.
 */
import { enableAutoUnmount, flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Grant } from "@/api/client";

enableAutoUnmount(afterEach);

// openapi-fetch takes fetch when the client is made, at import, so the stub has to be
// in place before any module loads; vi.hoisted runs first. Groups and grants answer
// from whatever the test sets, and each request's query is kept.
//
const stub = vi.hoisted(() => {
  const state = { groups: [] as unknown[], grants: [] as unknown[], asked: [] as URLSearchParams[] };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      state.asked.push(url.searchParams);
      const body = url.pathname.endsWith("/grants/groups") ? { groups: state.groups } : { grants: state.grants };
      return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
    }),
  );
  return state;
});

const { useGrants, NONE } = await import("@/stores/grants");
const GrantsView = (await import("@/views/GrantsView.vue")).default;

const ENDED = "3f9a61c2" + "0".repeat(56);

function grant(id: string, resource: string, session: string, lasts: Grant["lasts"] = "session"): Grant {
  return {
    id,
    principal: "claude-code",
    pattern: { id: "p", summary: `Mounts of ${resource}`, access: "mount", resource, match: {} },
    lasts,
    session,
    session_name: "laughing-sparky-daisy",
    granted_at: "2026-11-11T09:00:00+00:00",
  } as Grant;
}

describe("grants grouped by session", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
    stub.asked.length = 0;
    stub.groups = [
      { key: ENDED, session_name: "laughing-sparky-daisy", count: 2, permanent: 0 },
      { key: NONE, session_name: null, count: 1, permanent: 1 },
    ];
    // The third was made in the same session, but lasts until revoked, so it isn't the
    // session's to hold.
    //
    stub.grants = [
      grant("g1", "~/src/api", ENDED),
      grant("g2", "~/src/web", ENDED),
      grant("g3", "~/notes", ENDED, "permanent"),
    ];
  });

  it("names each session's group as a session is named everywhere, and the rest in words", async () => {
    const grants = useGrants();
    grants.by = "session";
    const page = mount(GrantsView, { attachTo: document.body });
    await flushPromises();
    const [ended, none] = page.findAll(".groups > li");
    expect(ended!.find(".key .name").text()).toBe("laughing-sparky-daisy");
    expect(ended!.find(".key code").text()).toBe("3f9a61c2");
    expect(ended!.find(".revoke-group").text()).toBe("Revoke all 2");
    expect(none!.find(".key").text()).toBe("Not held for a session");
    expect(none!.find(".revoke-group").exists()).toBe(false);

    await ended!.get(".revoke-group button").trigger("click");
    expect(ended!.find(".revoke-group").text()).toContain("Revoke all 2 for session laughing-sparky-daisy?");
  });

  it("lists one session's grants by its key, and those it doesn't hold by themselves", async () => {
    const grants = useGrants();
    grants.by = "session";
    grants.group = ENDED;
    await grants.load();
    expect(stub.asked.at(-1)!.get("session")).toBe(ENDED);

    grants.group = NONE;
    await grants.load();
    expect(stub.asked.at(-1)!.get("session")).toBeNull();
    expect(grants.grants.map((g) => g.id)).toEqual(["g3"]);
  });
});
