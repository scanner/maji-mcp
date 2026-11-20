/**
 * Staying unlocked while the tab is in use: the tab tells the control plane it is in
 * use at most once a minute, and only after input; it locks itself once unused for
 * the idle limit; and it drops its key when the control plane says it has locked.
 */
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// openapi-fetch takes fetch when the client is made, at import, so the stub has to be
// in place before any module loads; vi.hoisted runs first.
//
const stub = vi.hoisted(() => {
  const answers = new Map<string, () => Response>();
  const sent: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const key = `${request.method} ${new URL(request.url).pathname}`;
      sent.push(key);
      const answer = answers.get(key);
      if (!answer) throw new Error(`unexpected ${key}`);
      return answer();
    }),
  );
  return { answers, sent };
});

vi.mock("@/api/webauthn", () => ({ sign: vi.fn(async () => ({ id: "passkey" })) }));
vi.mock("@/api/tabkey", () => ({
  makeTabKey: vi.fn(async () => ({ pair: {}, jwk: { kty: "EC" } })),
  signWith: vi.fn(async () => "tab-signature"),
}));

const { needsPasskey, useUnlock } = await import("@/stores/unlock");
const { useAnnouncer } = await import("@/stores/announcer");

function json(body: unknown, status = 200): () => Response {
  const type = status >= 400 ? "application/problem+json" : "application/json";
  return () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": type } });
}

const ACTIVE = "POST /api/v0/unlocks/tab-1/active";
const SECOND = 1000;

describe("an unlocked tab", () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: new Date("2026-11-08T12:00:00Z") });
    setActivePinia(createPinia());
    stub.answers.clear();
    stub.sent.length = 0;
    stub.answers.set(
      "POST /api/v0/unlocks/options",
      json({ challenge_id: "u", nonce: "n", idle_seconds: 600, public_key: { challenge: "AQID" } }),
    );
    stub.answers.set(
      "POST /api/v0/unlocks",
      json({ id: "tab-1", idle_seconds: 600, ends_at: "2026-11-08T20:00:00Z" }, 201),
    );
    stub.answers.set(ACTIVE, () => new Response(null, { status: 204 }));
    stub.answers.set("DELETE /api/v0/unlocks/tab-1", () => new Response(null, { status: 204 }));
  });
  afterEach(() => {
    useUnlock().forget();
    vi.useRealTimers();
  });

  it("says it is in use once a minute after input, and not without it", async () => {
    const unlock = useUnlock();
    await unlock.unlock();

    await vi.advanceTimersByTimeAsync(SECOND);
    window.dispatchEvent(new Event("pointerdown"));
    await vi.advanceTimersByTimeAsync(60 * SECOND);
    expect(stub.sent.filter((s) => s === ACTIVE)).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(60 * SECOND);
    expect(stub.sent.filter((s) => s === ACTIVE)).toHaveLength(1);
    expect(unlock.unlocked).toBe(true);
  });

  it("stays unlocked while in use past the idle limit, and locks once unused for it", async () => {
    const unlock = useUnlock();
    await unlock.unlock();
    for (let minute = 0; minute < 15; minute++) {
      window.dispatchEvent(new Event("keydown"));
      await vi.advanceTimersByTimeAsync(60 * SECOND);
    }
    expect(unlock.unlocked).toBe(true);
    // The control plane keeps the same limit, so it has to hear about the use too.
    //
    expect(stub.sent.filter((s) => s === ACTIVE).length).toBeGreaterThanOrEqual(14);

    await vi.advanceTimersByTimeAsync(10 * 60 * SECOND);
    expect(unlock.unlocked).toBe(false);
    expect(stub.sent).toContain("DELETE /api/v0/unlocks/tab-1");
    expect(useAnnouncer().status).toContain("Deciding locked");
  });

  it("shows the time left, warns once in the last minute, and starts again on use", async () => {
    const unlock = useUnlock();
    await unlock.unlock();
    expect(unlock.left).toBe("locks in 10 min");

    await vi.advanceTimersByTimeAsync(4 * 60 * SECOND);
    expect(unlock.left).toBe("locks in 6 min");
    await vi.advanceTimersByTimeAsync(5 * 60 * SECOND + 5 * SECOND);
    expect(unlock.ending).toBe(true);
    expect(unlock.left).toBe("locks in under a minute");
    await vi.advanceTimersByTimeAsync(SECOND);
    expect(useAnnouncer().status).toBe("Deciding locks in under a minute unless you use this tab.");

    window.dispatchEvent(new Event("pointermove"));
    expect(unlock.ending).toBe(false);
    expect(unlock.left).toBe("locks in 10 min");
  });

  it("drops its key, and says so, when the control plane says it has locked", async () => {
    const unlock = useUnlock();
    await unlock.unlock();
    stub.answers.set(
      ACTIVE,
      json({ title: "locked", status: 403, code: "permission_denied", data: { reason: "locked" } }, 403),
    );
    await vi.advanceTimersByTimeAsync(SECOND);
    window.dispatchEvent(new Event("wheel"));
    await vi.advanceTimersByTimeAsync(61 * SECOND);

    expect(unlock.unlocked).toBe(false);
    await expect(unlock.signChallenge("AQID")).rejects.toThrow("isn't unlocked");
    expect(useAnnouncer().status).toContain("Deciding locked");
  });
});

describe("which decisions need a passkey touch in an unlocked tab", () => {
  const risky = { kind: "promote", risk: "high" } as never;
  const base = { item: "i", content_hash: `sha256:${"a".repeat(64)}` };

  it("a high-risk item's decision does, and taking a decision back on it doesn't", () => {
    expect(needsPasskey({ ...base, action: "promote" }, risky)).toBe(true);
    expect(needsPasskey({ ...base, action: "demote", supersedes: "d", note: "Why." }, risky)).toBe(false);
  });

  it("a choice taking a decision back that remembers anything still does", () => {
    const remembering = {
      ...base,
      action: "demote",
      supersedes: "d",
      remember: [{ pattern: "p1", lasts: "permanent" as const }],
    };
    expect(needsPasskey(remembering, risky)).toBe(true);
  });
});
