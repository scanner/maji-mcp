/**
 * Choices kept in this browser only: whether single-key shortcuts are on, and whether
 * deciding stays unlocked while you're using the tab.
 *
 * Single-key shortcuts can clash with a screen reader's own keys, so they can be
 * turned off (WCAG 2.1.4), and they only act while focus is in a list.
 */
import { defineStore } from "pinia";
import { ref, watch } from "vue";

const SHORTCUTS = "maji-mcp.shortcuts";
const STAY_UNLOCKED = "maji-mcp.stay-unlocked";

function stored(key: string): boolean {
  try {
    return localStorage.getItem(key) !== "off";
  } catch {
    return true;
  }
}

function keep(key: string, on: boolean): void {
  try {
    localStorage.setItem(key, on ? "on" : "off");
  } catch {
    // A browser that keeps nothing still works; the choice lasts this visit.
  }
}

export const usePreferences = defineStore("preferences", () => {
  const shortcuts = ref(stored(SHORTCUTS));
  const stayUnlocked = ref(stored(STAY_UNLOCKED));
  watch(shortcuts, (on) => keep(SHORTCUTS, on));
  watch(stayUnlocked, (on) => keep(STAY_UNLOCKED, on));
  return { shortcuts, stayUnlocked };
});
