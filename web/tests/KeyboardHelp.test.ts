/**
 * The keyboard help: each action once, with every key that does it, and the
 * single-key shortcuts listed only while they're on.
 */
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it } from "vitest";

import KeyboardHelp from "@/components/KeyboardHelp.vue";
import { usePreferences } from "@/stores/preferences";

import { violations } from "./axe";

function rows(help: ReturnType<typeof mount>): string[] {
  return help.findAll("tbody tr").map((row) =>
    row
      .findAll("th, td")
      .map((cell) => cell.text().replace(/\s+/g, " "))
      .join(" | "),
  );
}

describe("the keyboard help", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    localStorage.clear();
    document.body.innerHTML = "";
  });

  it("lists each action once, with its single-key shortcut while those are on", async () => {
    usePreferences().shortcuts = true;
    const help = mount(KeyboardHelp, { attachTo: document.body });
    expect(rows(help)).toEqual([
      "Next item | Control+N, ↓ or j",
      "Previous item | Control+P, ↑ or k",
      "First item | Option+< or Home",
      "Last item | Option+> or End",
      "Show the item | Enter",
      "Select or unselect it for a batch | Space on its box or x",
    ]);
    help.element.setAttribute("open", "");
    expect(await violations(help.element)).toEqual([]);

    await help.find(".toggle input").setValue(false);
    expect(rows(help)[0]).toBe("Next item | Control+N or ↓");
    expect(rows(help)[5]).toBe("Select or unselect it for a batch | Space on its box");
  });
});
