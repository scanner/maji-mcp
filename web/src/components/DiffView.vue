<script setup lang="ts">
import { computed } from "vue";

import type { Schemas } from "@/api/client";
import { isNewFile } from "@/lib/describe";

const props = defineProps<{ diff: NonNullable<Schemas["Diff"]> }>();

const SAID: Record<string, string> = { add: "Added", delete: "Removed", context: "Unchanged" };
const MARK: Record<string, string> = { add: "+", delete: "−", context: " " };

// A new file, such as a skill's code, says so once, in its caption, rather than
// marking every line as added, and reads as code.
//
const whole = computed(() => isNewFile(props.diff));

/** A line's leading whitespace, kept apart so a wrapped line hangs from its indent. */
function indent(text: string): string {
  return /^[ \t]*/.exec(text)?.[0] ?? "";
}
</script>

<template>
  <figure class="diff" :class="{ whole }">
    <figcaption>
      {{ whole ? "New file" : "Change to" }} <code>{{ diff.target }}</code>
    </figcaption>
    <div class="scroll">
      <table>
        <caption class="visually-hidden">
          {{ whole ? `Every line of ${diff.target}, all of it new` : `Lines added, removed and unchanged in ${diff.target}` }}
        </caption>
        <tbody>
          <tr v-for="(line, index) in diff.lines" :key="index" :class="[line.op, { flagged: line.flag }]">
            <th v-if="!whole" scope="row" class="mark">
              <span aria-hidden="true">{{ MARK[line.op] }}</span>
              <span class="visually-hidden">{{ SAID[line.op] }}{{ line.flag ? ", worth a look" : "" }}:</span>
            </th>
            <td>
              <span v-if="whole && line.flag" class="visually-hidden">Worth a look: </span>
              <code
                ><span class="lead">{{ indent(line.text) }}</span
                ><span class="rest">{{ line.text.slice(indent(line.text).length) }}</span></code
              >
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </figure>
</template>

<style scoped src="./DiffView.css"></style>
