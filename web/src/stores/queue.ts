/**
 * The queue: what's waiting (ahead), what's been decided (behind), the item in
 * front of you, the items selected for a batch, and deciding them: with a passkey, or
 * with the tab's own key while deciding is unlocked in it (`stores/unlock.ts`).
 *
 * Live updates come from the control plane's event stream. Events carry only ids, so
 * the store reloads the lanes when they arrive, at most once a moment.
 */
import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { ApiError, api, ok } from "@/api/client";
import type { Decision, DecisionChoice, Item, Pasted } from "@/api/client";
import { sign } from "@/api/webauthn";
import { DECIDED } from "@/lib/describe";
import { useAnnouncer } from "@/stores/announcer";
import { usePreferences } from "@/stores/preferences";
import { lockedOut, needsPasskey, useUnlock } from "@/stores/unlock";

const RELOAD_MS = 400;

function approvable(item: Item): boolean {
  return item.actions.some((a) => a.id === "approve" && !(a.needs ?? []).length);
}

/**
 * Why an item that could be approved as it stands is decided on its own, not in a
 * batch: a batch approves everything it asks for, just this once, which is wrong for
 * a request whose parts you may refuse one by one, an approval you may want
 * remembered, or a high-risk request.
 *
 * Returns:
 *     The reason, worded to follow a colon; null for an item a batch can take, or
 *     one that needs more than an approval anyway, such as a question
 */
export function aloneBecause(item: Item): string | null {
  if (!approvable(item)) return null;
  if (item.parts?.length) return "it asks for several things, and you may want to refuse some";
  if (item.patterns?.length) return "its approval can be remembered";
  if (item.risk === "high") return "it's high risk";
  return null;
}

/** An item can join a batch approval when approving it needs nothing more. */
export function batchable(item: Item): boolean {
  return approvable(item) && aloneBecause(item) === null;
}

/** The choice that approves an item as it stands, with nothing else chosen. */
export function approval(item: Item): DecisionChoice {
  return { item: item.id, content_hash: item.content_hash, action: "approve" };
}

export const useQueue = defineStore("queue", () => {
  const announcer = useAnnouncer();
  const ahead = ref<Item[]>([]);
  const behind = ref<Item[]>([]);
  const currentId = ref<string | null>(null);
  const selected = ref(new Set<string>());
  const loaded = ref(false);
  const deciding = ref(false);

  const current = computed<Item | null>(
    () =>
      ahead.value.find((i) => i.id === currentId.value) ??
      behind.value.find((i) => i.id === currentId.value) ??
      null,
  );
  const selectedItems = computed(() => ahead.value.filter((i) => selected.value.has(i.id)));

  async function load(): Promise<void> {
    const [waiting, decided] = await Promise.all([
      api.GET("/items", { params: { query: { lane: "ahead", limit: 200 } } }),
      api.GET("/items", { params: { query: { lane: "behind", limit: 50 } } }),
    ]);
    ahead.value = ok(waiting).items;
    behind.value = ok(decided).items;
    for (const id of [...selected.value]) {
      if (!ahead.value.some((i) => i.id === id)) selected.value.delete(id);
    }
    if (currentId.value === null && ahead.value[0]) currentId.value = ahead.value[0].id;
    loaded.value = true;
  }

  function open(id: string): void {
    currentId.value = id;
  }

  function toggle(id: string): void {
    if (selected.value.has(id)) selected.value.delete(id);
    else selected.value.add(id);
  }

  function clearSelection(): void {
    selected.value.clear();
  }

  /** After deciding, put the next waiting item in front of you. */
  function advance(decided: string[]): void {
    if (currentId.value === null || !decided.includes(currentId.value)) return;
    const next = ahead.value.find((i) => !decided.includes(i.id));
    currentId.value = next ? next.id : null;
  }

  async function challenge(choices: DecisionChoice[]) {
    return ok(await api.POST("/decisions/challenge", { body: { decisions: choices } }));
  }

  async function withPasskey(
    choices: DecisionChoice[],
    pasted?: Pasted,
  ): Promise<Decision[] | null> {
    const issued = await challenge(choices);
    const assertion = await sign(issued.public_key);
    if (assertion === null) return null;
    const result = await api.POST("/decisions", {
      body: {
        challenge_id: issued.challenge_id,
        assertion: assertion as never,
        ...(pasted ? { secrets: pasted } : {}),
      },
    });
    return ok(result).decisions;
  }

  /**
   * Decide with the tab's key, unlocking the tab first if it isn't: that is the one
   * passkey touch. A tab the control plane says has locked is unlocked once more.
   */
  async function withTab(choices: DecisionChoice[]): Promise<Decision[] | null> {
    const unlock = useUnlock();
    for (let attempt = 0; ; attempt++) {
      if (!unlock.unlocked && !(await unlock.unlock())) return null;
      const issued = await challenge(choices);
      const signed = await unlock.signChallenge(
        (issued.public_key as { challenge: string }).challenge,
      );
      try {
        const result = await api.POST("/decisions", {
          body: { challenge_id: issued.challenge_id, unlock: signed },
        });
        return ok(result).decisions;
      } catch (error) {
        if (!lockedOut(error) || attempt > 0) throw error;
        unlock.forget();
      }
    }
  }

  /**
   * Decide one or more items with at most one passkey touch: none while deciding is
   * unlocked in this tab, unless a decision lasts beyond its run or session.
   *
   * Args:
   *     choices: one decision for each item
   *     pasted: the key pasted into a Connect item, which goes with the decision
   *         and is kept nowhere; connecting always takes the passkey
   *
   * Returns:
   *     The decisions recorded, or null when nothing was decided; why is announced
   */
  async function decide(choices: DecisionChoice[], pasted?: Pasted): Promise<Decision[] | null> {
    deciding.value = true;
    try {
      const touch =
        pasted !== undefined ||
        choices.some((c) => needsPasskey(c, ahead.value.find((i) => i.id === c.item)));
      const made =
        usePreferences().stayUnlocked && !touch
          ? await withTab(choices)
          : await withPasskey(choices, pasted);
      if (made === null) {
        announcer.announce("Nothing was decided: the passkey prompt was closed.");
        return null;
      }
      const ids = made.map((d) => d.item);
      // A decision taking back an earlier one leaves its item open where you read it.
      advance(made.filter((d) => !d.supersedes).map((d) => d.item));
      for (const id of ids) selected.value.delete(id);
      await load();
      announcer.announce(summary(made));
      return made;
    } catch (error) {
      await explain(error);
      return null;
    } finally {
      deciding.value = false;
    }
  }

  /**
   * Let the requester ask again for the mounts you refused in a decided item. It
   * grants nothing: the agent's next run asking for them comes to you as a new item.
   */
  async function askAgain(id: string): Promise<void> {
    deciding.value = true;
    try {
      const item = ok(await api.POST("/items/{item_id}/ask-again", { params: { path: { item_id: id } } }));
      behind.value = behind.value.map((i) => (i.id === id ? item : i));
      announcer.announce("The agent can ask again for what you refused; it comes to you as a new item.");
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        announcer.warn("Your session has ended. Sign in again.");
      } else {
        const why = error instanceof ApiError ? error.message : "the control plane couldn't be reached";
        announcer.warn(`It still can't ask again: ${why}.`);
      }
    } finally {
      deciding.value = false;
    }
  }

  async function explain(error: unknown): Promise<void> {
    if (!(error instanceof ApiError)) {
      announcer.warn("The decision couldn't be sent. Check that the control plane is running.");
      return;
    }
    if (error.status === 409) {
      await load();
      announcer.warn(
        "Nothing was decided: an item changed or was decided since you saw it. " +
          "The queue has been reloaded; look again before deciding.",
      );
    } else if (error.status === 410) {
      announcer.warn("Nothing was decided: the passkey took too long. Try again.");
    } else if (lockedOut(error)) {
      announcer.warn("Nothing was decided: deciding locked in this tab. Try again to unlock it.");
    } else if (error.status === 403) {
      announcer.warn("Nothing was decided: the passkey wasn't accepted.");
    } else if (error.status === 401) {
      announcer.warn("Your session has ended. Sign in again to decide.");
    } else {
      announcer.warn(`Nothing was decided: ${error.message}.`);
    }
  }

  let source: EventSource | null = null;
  let reload: ReturnType<typeof setTimeout> | undefined;
  let arrived = 0;

  /** Follow the control plane's events until stop() is called. */
  function listen(): void {
    if (source !== null) return;
    source = new EventSource("/api/v0/events");
    const soon = (created: boolean) => () => {
      if (created) arrived += 1;
      clearTimeout(reload);
      reload = setTimeout(async () => {
        const count = arrived;
        arrived = 0;
        await load();
        if (count) announcer.announce(`${count} new ${count === 1 ? "item" : "items"} waiting.`);
      }, RELOAD_MS);
    };
    source.addEventListener("item.created", soon(true));
    for (const type of ["item.updated", "item.decided", "item.expired", "item.withdrawn", "resync"]) {
      source.addEventListener(type, soon(false));
    }
  }

  function stop(): void {
    source?.close();
    source = null;
    clearTimeout(reload);
  }

  return {
    ahead,
    behind,
    current,
    currentId,
    selected,
    selectedItems,
    loaded,
    deciding,
    load,
    open,
    toggle,
    clearSelection,
    decide,
    askAgain,
    listen,
    stop,
  };
});

/** One sentence saying what was decided, for screen readers and the status line. */
export function summary(decisions: Decision[]): string {
  if (decisions.length === 1 && decisions[0]) {
    return `${DECIDED[decisions[0].action] ?? "Decided"}. It's in the history.`;
  }
  return `${decisions.length} decisions recorded.`;
}
