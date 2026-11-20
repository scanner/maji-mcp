<script setup lang="ts">
/**
 * A session, as people see it: its name in three words, and beside it the start of its
 * key, which is what its grants are held to. Each is kept on one line, and set in its
 * own colour so it can be found at a glance; the words say what it is.
 */
import { shortKey } from "@/lib/describe";

defineProps<{
  /** The session's key. */
  session: string;
  /** Its name, from the control plane; without one, the whole key is shown. */
  name?: string | null;
  /**
   * Whether the start of the key is left out for screen readers, as in a list's rows,
   * where the name tells sessions apart; the item gives it.
   */
  quietKey?: boolean;
}>();
</script>

<template>
  <span class="session"
    ><template v-if="name"
      ><span class="name">{{ name }}</span> <code class="key" :aria-hidden="quietKey ? 'true' : undefined">{{
        shortKey(session)
      }}</code></template
    ><code v-else class="key">{{ session }}</code></span
  >
</template>

<style scoped>
.name,
.key {
  padding: 0 var(--space-1);
  border-radius: 4px;
  white-space: nowrap;
}
/* Not bold: bold on an item marks who asked, which is checked; a session's name is a
   label. */
.name {
  color: var(--session);
  background: var(--session-soft);
}
.key {
  color: var(--muted);
  background: var(--surface-sunken);
}
.key:only-child {
  white-space: normal;
  overflow-wrap: anywhere;
}
</style>
