/**
 * Deciding without a passkey prompt while you're using this tab.
 *
 * One passkey touch certifies a key the tab made (`api/tabkey.ts`); from then on the
 * tab signs each decision with that key. It stays unlocked while you use the tab, and
 * locks once you haven't pressed a key, clicked, scrolled or moved the pointer in it
 * for the idle limit the control plane set. While you're using it, the tab tells the
 * control plane so at most once a minute, since the control plane keeps the same limit.
 * How long is left is kept here for the header to show, with a warning, said once,
 * in the last minute.
 *
 * The key lives only in this store's closure, never in reactive state or storage, so
 * it is gone with the page.
 */
import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { ApiError, api, ok } from "@/api/client";
import type { DecisionChoice, Item } from "@/api/client";
import { makeTabKey, signWith } from "@/api/tabkey";
import type { TabKey } from "@/api/tabkey";
import { sign } from "@/api/webauthn";
import { useAnnouncer } from "@/stores/announcer";

const REPORT_MS = 60_000;
const TICK_MS = 5_000;
const WARN_SECONDS = 60;
const ACTIVITY = ["keydown", "pointerdown", "pointermove", "wheel", "scroll"] as const;
const LOCKED = "Deciding locked: the tab went unused. Your passkey unlocks it again.";

/**
 * Whether a decision asks for the passkey even in an unlocked tab: it remembers an
 * approval until a time or permanently, or connects a service, which last beyond the
 * run or session; or it decides a high-risk item. The control plane refuses these
 * without the passkey. Taking a decision back, such as demoting a skill, carries a
 * note at most and only narrows what agents can do, so it never does.
 */
export function needsPasskey(choice: DecisionChoice, item: Item | undefined): boolean {
  if (choice.supersedes !== undefined && !choice.remember && !choice.parts && !choice.answer) {
    return false;
  }
  const lasting = (choice.remember ?? []).some((r) => r.lasts === "until" || r.lasts === "permanent");
  return lasting || item?.kind === "connect" || item?.risk === "high";
}

/** Whether the control plane refused a tab's signature because the tab has locked. */
export function lockedOut(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 403 &&
    (error.data as { reason?: string } | undefined)?.reason === "locked"
  );
}

export const useUnlock = defineStore("unlock", () => {
  const announcer = useAnnouncer();
  const id = ref<string | null>(null);
  const idleSeconds = ref(600);
  const unlocked = computed(() => id.value !== null);
  /** Seconds until the tab locks: its idle limit, or its session's end if sooner. */
  const remaining = ref(0);
  const ending = computed(() => unlocked.value && remaining.value <= WARN_SECONDS);
  const left = computed(() =>
    ending.value ? "locks in under a minute" : `locks in ${Math.ceil(remaining.value / 60)} min`,
  );

  let key: TabKey | null = null;
  let endsAt = 0;
  let lastInput = 0;
  let lastReport = 0;
  let lastRefresh = 0;
  let warned = false;
  let timer: ReturnType<typeof setInterval> | undefined;

  function refresh(now = Date.now()): void {
    lastRefresh = now;
    const until = Math.min(lastInput + idleSeconds.value * 1000, endsAt);
    remaining.value = Math.max(0, Math.floor((until - now) / 1000));
    if (remaining.value > WARN_SECONDS) warned = false;
    else if (!warned && remaining.value > 0) {
      warned = true;
      announcer.announce("Deciding locks in under a minute unless you use this tab.");
    }
  }

  // Input arrives many times a second while the pointer moves, so the time left is
  // recomputed at most once a second.
  //
  function activity(): void {
    const now = Date.now();
    lastInput = now;
    if (id.value !== null && now - lastRefresh >= 1000) refresh(now);
  }

  function watchActivity(): void {
    for (const type of ACTIVITY) {
      window.addEventListener(type, activity, { capture: true, passive: true });
    }
    clearInterval(timer);
    timer = setInterval(() => void tick(), TICK_MS);
  }

  function stopWatching(): void {
    for (const type of ACTIVITY) window.removeEventListener(type, activity, { capture: true });
    clearInterval(timer);
    timer = undefined;
  }

  /**
   * Unlock deciding in this tab with one passkey touch.
   *
   * Returns:
   *     True once unlocked; false if the passkey prompt was closed
   */
  async function unlock(): Promise<boolean> {
    const made = await makeTabKey();
    const options = ok(await api.POST("/unlocks/options", { body: { key: made.jwk as never } }));
    const assertion = await sign(options.public_key);
    if (assertion === null) return false;
    const result = ok(
      await api.POST("/unlocks", {
        body: { challenge_id: options.challenge_id, assertion: assertion as never },
      }),
    );
    key = made;
    id.value = result.id;
    idleSeconds.value = result.idle_seconds;
    endsAt = Date.parse(result.ends_at);
    lastInput = lastReport = Date.now();
    warned = false;
    refresh();
    watchActivity();
    announcer.announce(
      `Deciding is unlocked in this tab until it goes unused for ${minutes(result.idle_seconds)}.`,
    );
    return true;
  }

  /** This tab's signature of a challenge, to send in place of an assertion. */
  async function signChallenge(challenge: string): Promise<{ id: string; signature: string }> {
    if (key === null || id.value === null) throw new Error("deciding isn't unlocked in this tab");
    activity();
    return { id: id.value, signature: await signWith(key, challenge) };
  }

  /** Drop the key, as when the control plane says the tab has locked. */
  function forget(): void {
    key = null;
    id.value = null;
    stopWatching();
  }

  async function lock(): Promise<void> {
    const was = id.value;
    forget();
    if (was === null) return;
    try {
      await api.DELETE("/unlocks/{unlock_id}", { params: { path: { unlock_id: was } } });
    } catch {
      // The key is gone from the page either way; the control plane's copy idles out.
    }
  }

  /** Lock once the tab has gone unused too long; while it's in use, say so. */
  async function tick(): Promise<void> {
    if (id.value === null) return;
    const now = Date.now();
    refresh(now);
    if (remaining.value === 0) {
      await lock();
      announcer.announce(LOCKED);
      return;
    }
    if (lastInput > lastReport && now - lastReport >= REPORT_MS) {
      lastReport = now;
      const result = await api.POST("/unlocks/{unlock_id}/active", {
        params: { path: { unlock_id: id.value } },
      });
      if (!result.response.ok) {
        forget();
        announcer.announce(LOCKED);
      }
    }
  }

  return {
    id,
    idleSeconds,
    unlocked,
    remaining,
    ending,
    left,
    unlock,
    signChallenge,
    forget,
    lock,
    tick,
    activity,
  };
});

export function minutes(seconds: number): string {
  const count = Math.round(seconds / 60);
  return count === 1 ? "a minute" : `${count} minutes`;
}
