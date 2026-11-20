<script setup lang="ts">
import { computed, nextTick, ref } from "vue";

import type { Item } from "@/api/client";
import SessionName from "@/components/SessionName.vue";
import {
  ALONE_TAG,
  WAITING_TAG,
  ago,
  kindLabel,
  listedAt,
  riskTag,
  shortWhen,
  stateLabel,
  waitedOn,
} from "@/lib/describe";
import { lessMotion } from "@/lib/media";
import { useNow } from "@/lib/now";
import { useAnnouncer } from "@/stores/announcer";
import { usePreferences } from "@/stores/preferences";
import { aloneBecause, batchable } from "@/stores/queue";

const props = defineProps<{
  items: Item[];
  label: string;
  currentId: string | null;
  selected?: Set<string>;
  selectable?: boolean;
  /** Whether the item in front of you opens in its row, beneath its button. */
  inline?: boolean;
}>();
const emit = defineEmits<{ open: [id: string]; close: [id: string]; toggle: [id: string] }>();
defineSlots<{ detail(props: { item: Item }): unknown }>();
const preferences = usePreferences();
const announcer = useAnnouncer();
const list = ref<HTMLUListElement | null>(null);

// How long ago changes every second, so it is shown and not read out: a screen reader
// can read a focused row again each time its text changes. The time itself is read.
//
const now = useNow();

// The list is one stop in the Tab order: only one row's controls take Tab, so Tab and
// Shift-Tab move between the page's parts and an item's fields rather than through
// every item. That row is the one last focused, else the item in front of you.
//
const focusedId = ref<string | null>(null);
const tabRow = computed(() => {
  for (const id of [focusedId.value, props.currentId]) {
    const index = props.items.findIndex((i) => i.id === id);
    if (index >= 0) return index;
  }
  return 0;
});

// Vue doesn't keep refs gathered in a v-for in the list's order, so the rows are read
// from the DOM, which is.
//
function rows(): HTMLLIElement[] {
  return [...(list.value?.querySelectorAll<HTMLLIElement>(":scope > li") ?? [])];
}

/**
 * A row's own box or button. An open row also holds its item's detail, whose controls
 * are left alone, so only the row's direct children count.
 */
function control(row: HTMLLIElement, box: boolean): HTMLElement | null {
  return row.querySelector<HTMLElement>(box ? ":scope > input.pick" : ":scope > button.open");
}

/** Focus a row, in the same column as now: its box if focus is on a box, else its button. */
function focusAt(index: number, onBox: boolean): void {
  const all = rows();
  if (!all.length) return;
  const row = all[(index + all.length) % all.length]!;
  ((onBox ? control(row, true) : null) ?? control(row, false))?.focus();
}

type Move = "next" | "previous" | "first" | "last";

/** A key pressed alone, which counts only while single-key shortcuts are on. */
function single(event: KeyboardEvent): boolean {
  return preferences.shortcuts && !event.altKey && !event.ctrlKey && !event.metaKey;
}

/**
 * Which way a key moves: the arrows, Home and End; C-n, C-p, M-< and M-> as in Emacs;
 * and j and k while single-key shortcuts are on.
 *
 * M-< and M-> are matched on the physical key, since macOS's Option turns them into
 * other characters.
 */
function move(event: KeyboardEvent): Move | null {
  const ctrl = event.ctrlKey && !event.altKey && !event.metaKey;
  const meta = event.altKey && !event.ctrlKey && !event.metaKey;
  const key = event.key.toLowerCase();
  if (event.key === "ArrowDown" || (ctrl && key === "n") || (single(event) && event.key === "j")) {
    return "next";
  }
  if (event.key === "ArrowUp" || (ctrl && key === "p") || (single(event) && event.key === "k")) {
    return "previous";
  }
  if (event.key === "Home" || (meta && event.code === "Comma")) return "first";
  if (event.key === "End" || (meta && event.code === "Period")) return "last";
  return null;
}

function onKey(event: KeyboardEvent): void {
  // Only a row's own box and button move through the list: keys pressed in an open
  // item's fields, which sit deeper in the row, are theirs.
  //
  const target = event.target as HTMLElement;
  const index = rows().indexOf(target.parentElement as HTMLLIElement);
  const item = props.items[index];
  if (!item || !target.matches("input.pick, button.open")) return;
  const onBox = target.matches("input.pick");
  const way = move(event);
  if (way === "next") focusAt(index + 1, onBox);
  else if (way === "previous") focusAt(index - 1, onBox);
  else if (way === "first") focusAt(0, onBox);
  else if (way === "last") focusAt(rows().length - 1, onBox);
  else if (single(event) && event.key === "x" && props.selectable && batchable(item)) {
    emit("toggle", item.id);
  } else if (single(event) && event.key === "x" && props.selectable && aloneBecause(item)) {
    announcer.announce(`${item.title} is decided on its own: ${aloneBecause(item)}.`);
  } else if (event.key === "Enter" && onBox) {
    void choose(item.id, target);
  } else return;
  event.preventDefault();
}

/**
 * Open an item. In a list that opens items in place, the row chosen stays where it
 * was clicked: an item open above it closes at once, and the page scrolls by as much
 * as that moved the row, before the browser draws it.
 */
async function choose(id: string, control: HTMLElement): Promise<void> {
  const before = control.getBoundingClientRect().top;
  emit("open", id);
  if (!props.inline) return;
  await nextTick();
  const moved = control.getBoundingClientRect().top - before;
  if (moved) window.scrollBy({ top: moved, behavior: "instant" });
}

/** Close the open item from its end, and put focus back on its row's button. */
async function close(id: string, event: MouseEvent): Promise<void> {
  const row = (event.currentTarget as HTMLElement).closest("li");
  emit("close", id);
  await nextTick();
  row?.querySelector<HTMLElement>(":scope > button.open")?.focus();
}

const GROW_MS = 200;
const running = new WeakMap<Element, Animation>();

/**
 * Grow an item's detail open beneath its row, or shrink it closed, then call `done`.
 * The detail's grid row goes between 0fr and 1fr, which animates its height without
 * measuring it. With less motion asked for, or no Web Animations, it changes at once.
 */
function animate(panel: Element, opening: boolean, done: () => void): void {
  if (lessMotion() || typeof (panel as HTMLElement).animate !== "function") {
    done();
    return;
  }
  const shut = { gridTemplateRows: "0fr", opacity: 0 };
  const open = { gridTemplateRows: "1fr", opacity: 1 };
  panel.classList.add("moving");
  const run = (panel as HTMLElement).animate(opening ? [shut, open] : [open, shut], {
    duration: GROW_MS,
    easing: "cubic-bezier(0.2, 0.7, 0.2, 1)",
  });
  running.set(panel, run);
  run.onfinish = () => {
    panel.classList.remove("moving");
    running.delete(panel);
    done();
  };
}

/**
 * Once open, scroll as little as shows the whole item, or its top if it's taller than
 * the window. With no animation this runs as the item is put in, so it waits a frame
 * to come after `choose` has put the row back where it was clicked.
 */
function grow(panel: Element, done: () => void): void {
  animate(panel, true, () => {
    done();
    requestAnimationFrame(() =>
      panel.closest("li")?.scrollIntoView?.({ block: "nearest", behavior: lessMotion() ? "auto" : "smooth" }),
    );
  });
}

/** An item closed by opening another closes at once (see `choose`); one just closed shrinks. */
function shrink(panel: Element, done: () => void): void {
  if (props.currentId !== null) done();
  else animate(panel, false, done);
}

function stop(panel: Element): void {
  running.get(panel)?.cancel();
  running.delete(panel);
  panel.classList.remove("moving");
}
</script>

<template>
  <p v-if="!items.length" class="empty">Nothing here.</p>
  <ul v-else ref="list" class="items" :aria-label="label" @keydown="onKey">
    <li
      v-for="(item, index) in items"
      :key="item.id"
      :class="{ current: item.id === currentId, blocking: waitedOn(item) }"
      @focusin="focusedId = item.id"
    >
      <input
        v-if="selectable && batchable(item)"
        :id="`select-${item.id}`"
        type="checkbox"
        class="pick"
        :tabindex="index === tabRow ? 0 : -1"
        :checked="selected?.has(item.id)"
        @change="emit('toggle', item.id)"
      />
      <label v-if="selectable && batchable(item)" :for="`select-${item.id}`" class="visually-hidden">
        Select for a batch approval: {{ item.title }}
      </label>
      <span v-else-if="selectable" class="pick-gap" aria-hidden="true"></span>
      <button
        type="button"
        class="open"
        :tabindex="index === tabRow ? 0 : -1"
        :aria-current="!inline && item.id === currentId ? 'true' : undefined"
        :aria-expanded="inline ? item.id === currentId : undefined"
        :aria-controls="inline && item.id === currentId ? `inline-${item.id}` : undefined"
        @click="choose(item.id, $event.currentTarget as HTMLElement)"
      >
        <span class="title">{{ item.title }}</span>
        <span v-if="item.subject" class="subject">{{ item.subject }}</span>
        <span class="tags">
          <span class="tag">{{ kindLabel(item) }}</span>
          <span v-if="riskTag(item)" class="tag risk-high">{{ riskTag(item) }}</span>
          <span v-if="waitedOn(item)" class="tag blocking-tag">{{ WAITING_TAG }}</span>
          <span v-if="selectable && aloneBecause(item)" class="tag">{{ ALONE_TAG }}</span>
          <span v-if="item.state !== 'waiting'" class="tag">{{ stateLabel(item) }}</span>
        </span>
        <span class="meta">
          <span class="when">
            {{ listedAt(item).label }}
            <time :datetime="listedAt(item).at">{{ shortWhen(listedAt(item).at, now) }}</time
            ><span aria-hidden="true">, <span class="ago">{{ ago(listedAt(item).at, now) }}</span></span>
          </span>
          <span v-if="item.requester.session"
            ><span class="visually-hidden">in session </span
            ><SessionName :session="item.requester.session" :name="item.session_name" quiet-key
          /></span>
        </span>
        <span v-if="inline" class="chevron" aria-hidden="true"></span>
      </button>
      <Transition v-if="inline" :css="false" @enter="grow" @leave="shrink" @enter-cancelled="stop" @leave-cancelled="stop">
        <div v-if="item.id === currentId" :id="`inline-${item.id}`" class="inline">
          <div class="clip">
            <div class="body">
              <slot name="detail" :item="item" />
              <button type="button" class="close" :aria-label="`Close: ${item.title}`" @click="close(item.id, $event)">
                Close
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </li>
  </ul>
</template>

<style scoped src="./ItemList.css"></style>
