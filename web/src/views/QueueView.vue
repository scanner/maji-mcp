<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import type { DecisionChoice, Pasted } from "@/api/client";
import BatchBar from "@/components/BatchBar.vue";
import ItemDetail from "@/components/ItemDetail.vue";
import ItemList from "@/components/ItemList.vue";
import { useMedia } from "@/lib/media";
import { useAnnouncer } from "@/stores/announcer";
import { approval, useQueue } from "@/stores/queue";

const queue = useQueue();
const announcer = useAnnouncer();
const failed = ref(false);
const showHistory = ref(false);

// After a decision, the next item's buttons stay inert for a moment. Without a
// passkey prompt between decisions, the second click of a double click would
// otherwise land on the next item's button in the same place.
//
const SETTLE_MS = 700;
const settling = ref(false);
let settle: ReturnType<typeof setTimeout> | undefined;

// Below this width the page is one column, and the item in front of you opens in its
// row, where you chose it, rather than below every list. There it can be closed, so
// that nothing is open; any other item opened, or put in front of you after a
// decision, opens.
//
const NARROW = "(max-width: 56rem)";
const narrow = useMedia(NARROW);
const shut = ref(false);
const openId = computed(() => (narrow.value && shut.value ? null : queue.currentId));
watch(
  () => queue.currentId,
  () => (shut.value = false),
);

/** Open an item; in a narrow window, opening the one already open closes it. */
function open(id: string): void {
  if (narrow.value && id === openId.value) {
    shut.value = true;
    return;
  }
  shut.value = false;
  queue.open(id);
}

onMounted(async () => {
  try {
    await queue.load();
    queue.listen();
  } catch {
    failed.value = true;
    announcer.warn("The queue couldn't be loaded. Is the control plane running?");
  }
});
onBeforeUnmount(() => {
  queue.stop();
  clearTimeout(settle);
});

/**
 * After a decision, the next item takes the old one's place, so focus moves to its
 * heading and its title is read out. Focus left on a button would sit on the next
 * item's button of the same name, and a second press would decide an item that was
 * never read.
 */
async function decided(made: unknown[] | null): Promise<void> {
  if (made === null) return;
  settling.value = true;
  clearTimeout(settle);
  settle = setTimeout(() => (settling.value = false), SETTLE_MS);
  await nextTick();
  const heading =
    (queue.currentId ? document.getElementById(`title-${queue.currentId}`) : null) ??
    document.querySelector<HTMLElement>("[data-page-heading]");
  heading?.focus();
  const next = queue.current?.title;
  announcer.announce(next ? `Next: ${next}${/[.?!]$/.test(next) ? "" : "."}` : "Nothing else is waiting.");
}

async function decide(choice: DecisionChoice, pasted?: Pasted): Promise<void> {
  const made = await queue.decide([choice], pasted);
  if (choice.supersedes === undefined) {
    await decided(made);
    return;
  }
  // Taking back a decided item's decision, such as demoting a skill, leaves the item
  // where you were reading it, at the top of the history, so focus stays on it.
  //
  if (made === null) return;
  await nextTick();
  document.getElementById(`title-${choice.item}`)?.focus();
}

async function approveSelected(): Promise<void> {
  await decided(await queue.decide(queue.selectedItems.map(approval)));
}
</script>

<template>
  <div class="queue" :class="{ narrow }">
    <h1 data-page-heading tabindex="-1">Queue</h1>
    <p v-if="failed" class="failed">The queue couldn't be loaded.</p>

    <BatchBar
      v-if="queue.selected.size"
      :count="queue.selected.size"
      :busy="queue.deciding || settling"
      @approve="approveSelected"
      @clear="queue.clearSelection()"
    />

    <div class="layout">
      <div class="lanes">
        <section aria-labelledby="waiting-title">
          <h2 id="waiting-title">Waiting <span class="count">({{ queue.ahead.length }})</span></h2>
          <ItemList
            :items="queue.ahead"
            label="Waiting for a decision"
            :current-id="openId"
            :selected="queue.selected"
            selectable
            :inline="narrow"
            @open="open"
            @close="shut = true"
            @toggle="queue.toggle"
          >
            <template #detail="{ item }">
              <ItemDetail
                :item="item"
                :busy="queue.deciding || settling"
                inline
                @decide="decide"
                @ask-again="queue.askAgain"
              />
            </template>
          </ItemList>
        </section>
        <section aria-labelledby="history-title">
          <h2 id="history-title">
            <button
              type="button"
              class="disclose"
              :aria-expanded="showHistory"
              aria-controls="history-list"
              @click="showHistory = !showHistory"
            >
              Decided <span class="count">({{ queue.behind.length }})</span>
            </button>
          </h2>
          <div v-show="showHistory" id="history-list">
            <ItemList
              :items="queue.behind"
              label="Decided, newest first"
              :current-id="openId"
              :inline="narrow"
              @open="open"
              @close="shut = true"
            >
              <template #detail="{ item }">
                <ItemDetail
                  :item="item"
                  :busy="queue.deciding || settling"
                  inline
                  @decide="decide"
                  @ask-again="queue.askAgain"
                />
              </template>
            </ItemList>
          </div>
        </section>
      </div>

      <section v-if="!narrow" class="now" aria-label="The item in front of you">
        <ItemDetail
          v-if="queue.current"
          :key="queue.current.id"
          :item="queue.current"
          :busy="queue.deciding || settling"
          @decide="decide"
          @ask-again="queue.askAgain"
        />
        <p v-else-if="queue.loaded" class="clear">Nothing is waiting for you.</p>
      </section>
    </div>
  </div>
</template>

<style scoped src="./QueueView.css"></style>
