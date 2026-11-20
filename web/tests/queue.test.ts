/**
 * The queue store's deciding: with the tab's key while deciding is unlocked, with the
 * passkey when a decision lasts or the tab isn't to stay unlocked, and what it says
 * when the control plane refuses. The demo can't produce these refusals, so the
 * control plane's answers are stubbed here.
 */
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { item } from "./fixtures";

// openapi-fetch takes fetch when the client is made, at import, so the stub has to be
// in place before any module loads; vi.hoisted runs first. Each route answers from a
// list, one answer per call, repeating the last; every request's body is kept.
//
const stub = vi.hoisted(() => {
  const routes = new Map<string, (() => Response)[]>();
  const sent: { key: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const key = `${request.method} ${url.pathname}${url.search.includes("behind") ? "?behind" : ""}`;
      const text = await request.clone().text();
      sent.push({ key, body: text ? JSON.parse(text) : undefined });
      const answers = routes.get(key);
      if (!answers?.length) throw new Error(`unexpected ${key}`);
      return (answers.length > 1 ? answers.shift()! : answers[0]!)();
    }),
  );
  return { routes, sent };
});

const touches = vi.hoisted(() => ({ count: 0 }));
vi.mock("@/api/webauthn", () => ({
  sign: vi.fn(async () => {
    touches.count += 1;
    return { id: "passkey" };
  }),
}));
vi.mock("@/api/tabkey", () => ({
  makeTabKey: vi.fn(async () => ({ pair: {}, jwk: { kty: "EC", crv: "P-256", x: "x", y: "y" } })),
  signWith: vi.fn(async () => "tab-signature"),
}));

const { useQueue } = await import("@/stores/queue");
const { useAnnouncer } = await import("@/stores/announcer");
const { usePreferences } = await import("@/stores/preferences");
const { useUnlock } = await import("@/stores/unlock");

function json(body: unknown, status = 200): () => Response {
  const type = status >= 400 ? "application/problem+json" : "application/json";
  return () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": type } });
}

function answer(key: string, ...responses: (() => Response)[]): void {
  stub.routes.set(key, responses);
}

const CHALLENGE = json({
  challenge_id: "c",
  nonce: "n",
  decision_hashes: [],
  public_key: { challenge: "AQID" },
});
const DECIDED = (id: string) =>
  json(
    {
      decisions: [
        {
          id: "d",
          item: id,
          content_hash: `sha256:${"a".repeat(64)}`,
          action: "approve",
          decision_hash: `sha256:${"c".repeat(64)}`,
          by: { kind: "you" },
          at: "2026-11-08T12:00:00Z",
        },
      ],
    },
    201,
  );
const LOCKED = json(
  { title: "deciding has locked", status: 403, code: "permission_denied", data: { reason: "locked" } },
  403,
);

async function queueOf(...items: ReturnType<typeof item>[]) {
  answer("GET /api/v0/items", json({ items, next_cursor: null }));
  answer("GET /api/v0/items?behind", json({ items: [], next_cursor: null }));
  answer("POST /api/v0/unlocks/options", json({
    challenge_id: "u",
    nonce: "n",
    idle_seconds: 600,
    public_key: { challenge: "BAUG" },
  }));
  answer("POST /api/v0/unlocks", json({ id: "tab-1", idle_seconds: 600, ends_at: "2026-11-08T20:00:00Z" }, 201));
  answer("POST /api/v0/decisions/challenge", CHALLENGE);
  const queue = useQueue();
  await queue.load();
  return queue;
}

function approve(of: ReturnType<typeof item>) {
  return { item: of.id, content_hash: of.content_hash, action: "approve" };
}

function decisionsSent() {
  return stub.sent.filter((s) => s.key === "POST /api/v0/decisions").map((s) => s.body);
}

describe("deciding", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    stub.routes.clear();
    stub.sent.length = 0;
    touches.count = 0;
  });
  afterEach(() => {
    useUnlock().forget();
  });

  it("decides twice on one passkey touch, signing with the unlocked tab's key", async () => {
    const first = item({ id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b81" });
    const second = item({ id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b82" });
    const queue = await queueOf(first, second);
    answer("POST /api/v0/decisions", DECIDED(first.id), DECIDED(second.id));

    expect(await queue.decide([approve(first)])).not.toBeNull();
    expect(await queue.decide([approve(second)])).not.toBeNull();

    expect(touches.count).toBe(1);
    expect(decisionsSent()).toEqual([
      { challenge_id: "c", unlock: { id: "tab-1", signature: "tab-signature" } },
      { challenge_id: "c", unlock: { id: "tab-1", signature: "tab-signature" } },
    ]);
  });

  it("asks for the passkey for a decision that lasts, even when unlocked", async () => {
    const mount = item({ kind: "grant" });
    const queue = await queueOf(mount);
    answer("POST /api/v0/decisions", DECIDED(mount.id));

    await queue.decide([{ ...approve(mount), remember: [{ pattern: "p1", lasts: "permanent" }] }]);

    expect(stub.sent.some((s) => s.key.startsWith("POST /api/v0/unlocks"))).toBe(false);
    expect(decisionsSent()).toEqual([{ challenge_id: "c", assertion: { id: "passkey" } }]);
  });

  it("asks for the passkey for a high-risk item, even when unlocked", async () => {
    const risky = item({
      risk: "high",
      risk_reasons: [
        { code: "addressed_to_ai", basis: "suspected", text: "a line is worded as an instruction", evidence: [{ fact: 0 }] },
      ],
    });
    const queue = await queueOf(risky);
    answer("POST /api/v0/decisions", DECIDED(risky.id));

    await queue.decide([approve(risky)]);

    expect(stub.sent.some((s) => s.key.startsWith("POST /api/v0/unlocks"))).toBe(false);
    expect(decisionsSent()).toEqual([{ challenge_id: "c", assertion: { id: "passkey" } }]);
  });

  it("unlocks once more when the tab has locked, and only once", async () => {
    const one = item();
    const queue = await queueOf(one);
    answer("POST /api/v0/decisions", LOCKED, DECIDED(one.id));
    expect(await queue.decide([approve(one)])).not.toBeNull();
    expect(touches.count).toBe(2);

    answer("POST /api/v0/decisions", LOCKED);
    useUnlock().forget();
    expect(await queue.decide([approve(one)])).toBeNull();
    expect(useAnnouncer().alert).toContain("deciding locked in this tab");
  });

  it("asks for the passkey every time while the tab isn't to stay unlocked", async () => {
    const one = item();
    const queue = await queueOf(one);
    usePreferences().stayUnlocked = false;
    answer("POST /api/v0/decisions", DECIDED(one.id));

    await queue.decide([approve(one)]);

    expect(useUnlock().unlocked).toBe(false);
    expect(decisionsSent()).toEqual([{ challenge_id: "c", assertion: { id: "passkey" } }]);
  });

  it("decides nothing when an item changed meanwhile, and reloads the queue", async () => {
    const before = item({ title: "As you saw it" });
    const after = item({ title: "As it is now", content_hash: `sha256:${"b".repeat(64)}` });
    const queue = await queueOf(before);
    answer("POST /api/v0/decisions", json({ title: "an item changed", status: 409, code: "conflict" }, 409));
    answer("GET /api/v0/items", json({ items: [after], next_cursor: null }));

    const made = await queue.decide([approve(before)]);
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(made).toBeNull();
    expect(useAnnouncer().alert).toContain("changed or was decided since you saw it");
    expect(queue.ahead.map((i) => i.title)).toEqual(["As it is now"]);
  });
});
