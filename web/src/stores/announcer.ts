/**
 * What screen readers hear: polite status messages, and assertive alerts for
 * failures. Messages that arrive close together are joined into one, so a burst of
 * updates is read once ("3 new items"), not once per event.
 */
import { defineStore } from "pinia";
import { ref } from "vue";

const GATHER_MS = 600;

export const useAnnouncer = defineStore("announcer", () => {
  const status = ref("");
  const alert = ref("");
  let pending: string[] = [];
  let timer: ReturnType<typeof setTimeout> | undefined;

  /** Say something politely, after whatever is being read now. */
  function announce(message: string): void {
    pending.push(message);
    clearTimeout(timer);
    timer = setTimeout(() => {
      status.value = pending.join(" ");
      pending = [];
    }, GATHER_MS);
  }

  /** Interrupt with a failure the person needs to know about at once. */
  function warn(message: string): void {
    alert.value = "";
    queueMicrotask(() => {
      alert.value = message;
    });
  }

  return { status, alert, announce, warn };
});
