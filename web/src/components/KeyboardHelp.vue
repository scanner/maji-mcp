<script setup lang="ts">
import { ref } from "vue";

import { usePreferences } from "@/stores/preferences";

const preferences = usePreferences();
const dialog = ref<HTMLDialogElement | null>(null);

function open(): void {
  dialog.value?.showModal();
}

defineExpose({ open });

/** One way to press for an action: keys held together, and where focus has to be. */
interface Press {
  keys: string[];
  where?: string;
  /** A single-key shortcut, listed only while those are on. */
  single?: boolean;
}

/** Each action in a list once, with every key that does it. */
const ACTIONS: { does: string; presses: Press[] }[] = [
  { does: "Next item", presses: [{ keys: ["Control", "N"] }, { keys: ["↓"] }, { keys: ["j"], single: true }] },
  { does: "Previous item", presses: [{ keys: ["Control", "P"] }, { keys: ["↑"] }, { keys: ["k"], single: true }] },
  { does: "First item", presses: [{ keys: ["Option", "<"] }, { keys: ["Home"] }] },
  { does: "Last item", presses: [{ keys: ["Option", ">"] }, { keys: ["End"] }] },
  { does: "Show the item", presses: [{ keys: ["Enter"] }] },
  {
    does: "Select or unselect it for a batch",
    presses: [{ keys: ["Space"], where: "on its box" }, { keys: ["x"], single: true }],
  },
];

function shown(presses: Press[]): Press[] {
  return presses.filter((p) => preferences.shortcuts || !p.single);
}

/** What goes before the press at `index` of `count`: nothing, a comma, or "or". */
function joiner(index: number, count: number): string {
  if (index === 0) return "";
  return index === count - 1 ? " or " : ", ";
}
</script>

<template>
  <dialog ref="dialog" aria-labelledby="keys-title" class="help">
    <h2 id="keys-title">Keyboard</h2>
    <p>
      <kbd>Tab</kbd> and <kbd>Shift</kbd>+<kbd>Tab</kbd> move between the page's parts and an
      item's fields; each list is one stop. In a list:
    </p>
    <table>
      <caption class="visually-hidden">Keys in a list</caption>
      <thead>
        <tr><th scope="col">To</th><th scope="col">Press</th></tr>
      </thead>
      <tbody>
        <tr v-for="action in ACTIONS" :key="action.does">
          <th scope="row">{{ action.does }}</th>
          <td>
            <template v-for="(press, index) in shown(action.presses)" :key="index"
              >{{ joiner(index, shown(action.presses).length)
              }}<template v-for="(key, k) in press.keys" :key="key"
                >{{ k ? "+" : "" }}<kbd>{{ key }}</kbd></template
              >{{ press.where ? ` ${press.where}` : "" }}</template
            >
          </td>
        </tr>
      </tbody>
    </table>
    <label class="toggle">
      <input v-model="preferences.shortcuts" type="checkbox" />
      <span>
        Single-key shortcuts <kbd>j</kbd>, <kbd>k</kbd> and <kbd>x</kbd> (turn them off if
        they clash with your screen reader)
      </span>
    </label>
    <form method="dialog">
      <button type="submit">Close</button>
    </form>
  </dialog>
</template>

<style scoped src="./HelpDialog.css"></style>
<style scoped src="./KeyboardHelp.css"></style>
