<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { Action, DecisionChoice, Item, Lasts, Pasted, Pattern, Remember } from "@/api/client";
import SessionName from "@/components/SessionName.vue";
import { APPROVING, NEEDS_A_NOTE, afterSlashes } from "@/lib/describe";
import { usePreferences } from "@/stores/preferences";
import { minutes, useUnlock } from "@/stores/unlock";

const props = defineProps<{ item: Item; busy: boolean }>();
const emit = defineEmits<{ decide: [choice: DecisionChoice, pasted?: Pasted] }>();

const refused = ref(new Set<string>());
const answerChoice = ref("");
const answerText = ref("");
// How long to remember each pattern, by its id: "" is just this once, and "hours" is
// MOUNT_HOURS from when you decide, within the requester's session when it named one.
type Keep = Lasts | "" | "hours";
const keep = ref<Record<string, Keep>>({});
const MOUNT_HOURS = 8;
const untils = ref<Record<string, string>>({});
const note = ref("");
const problem = ref("");
// The key pasted into a Connect item lives here, in this form alone, until the
// decision goes: never in a store, browser storage or the address.
const key = ref("");
const settings = ref<Record<string, string>>({});
const unlock = useUnlock();
const preferences = usePreferences();

watch(
  () => props.item.id,
  () => {
    refused.value = new Set();
    answerChoice.value = "";
    answerText.value = "";
    keep.value = Object.fromEntries((props.item.patterns ?? []).map((p) => [p.id, shortest(p)]));
    untils.value = {};
    note.value = "";
    problem.value = "";
    key.value = "";
    settings.value = Object.fromEntries(
      (props.item.connection?.settings ?? []).map((s) => [s.name, ""]),
    );
  },
  { immediate: true },
);

/**
 * Which session "For this session" means, said once beside the choices that offer it,
 * and read with each of them; null when there is no such choice.
 */
const thisSession = computed(() =>
  props.item.requester.session && props.item.patterns?.length ? `this-session-${props.item.id}` : null,
);

/** Whether this form takes back a decided item's decision, such as demoting a skill. */
const reconsidering = computed(() => props.item.state !== "waiting");
/**
 * The actions the form can carry out: connecting needs the service the item is for,
 * which an item made before connections were built doesn't name.
 */
const actions = computed<Action[]>(() =>
  props.item.actions.filter((a) => !(a.needs ?? []).includes("credential") || props.item.connection),
);

/** The service a waiting Connect item asks you to paste a key for. */
const connecting = computed(() =>
  props.item.kind === "connect" && !reconsidering.value ? props.item.connection ?? null : null,
);

// The note says which of this item's actions needs it, such as asking for changes.
//
const noteLabel = computed(() => {
  if (reconsidering.value) return "Why (optional): agents that try it see this";
  const needing = actions.value
    .filter((a) => (a.needs ?? []).includes("note"))
    .map((a) => NEEDS_A_NOTE[a.id] ?? `to ${a.label.toLowerCase()}`);
  return needing.length
    ? `Note to the requester (optional, except ${needing.join(" or ")})`
    : "Note to the requester (optional)";
});
/**
 * A mount is never approved just once: the agent's script runs again after you
 * decide, so only a remembered mount covers it. Nor for the rest of a run, which has
 * already ended.
 */
function isMount(pattern: Pattern): boolean {
  return pattern.access === "mount";
}

/**
 * The duration chosen until you choose another: just this once, or for a mount, this
 * session, as you set it (2026-11-09), or MOUNT_HOURS when there is no session.
 */
function shortest(pattern: Pattern): Keep {
  if (!isMount(pattern)) return "";
  return props.item.requester.session ? "session" : "hours";
}

/**
 * The durations a pattern can be remembered for. For a mount, MOUNT_HOURS ends the
 * session's grant early, since a session can last days, so it covers no more than
 * the session does. Without a session, it covers every run of the requester's.
 */
function durations(pattern: Pattern): { value: Keep; label: string }[] {
  const mount = isMount(pattern);
  const session = Boolean(props.item.requester.session);
  const options: { value: Keep; label: string }[] = [];
  if (!mount) options.push({ value: "", label: "Just this once" });
  if (props.item.requester.run && !mount) options.push({ value: "run", label: "For the rest of this run" });
  if (session) options.push({ value: "session", label: "For this session" });
  if (mount) {
    const label = session ? `For this session, ${MOUNT_HOURS} hours at most` : `For ${MOUNT_HOURS} hours`;
    options.push({ value: "hours", label });
  }
  options.push({ value: "until", label: "Until a time you choose" });
  options.push({ value: "permanent", label: "Always, until you revoke it" });
  return options;
}

const hasMounts = computed(() => (props.item.patterns ?? []).some(isMount));

/** The patterns that cover exactly one part, shown beside that part. */
function patternsFor(partId: string): Pattern[] {
  return (props.item.patterns ?? []).filter((p) => p.parts?.length === 1 && p.parts[0] === partId);
}

/** The patterns over the whole request, or over several of its parts. */
const broader = computed(() =>
  (props.item.patterns ?? []).filter((p) => !props.item.parts?.length || (p.parts?.length ?? 0) !== 1),
);

/** A pattern over a part you refused can't be remembered. */
function blocked(pattern: Pattern): boolean {
  return (pattern.parts ?? []).some((id) => refused.value.has(id));
}

/**
 * The patterns that each cover one part. With several, one choice above the parts
 * sets how long every one is remembered, so a request for nine paths is nine paths
 * decided once, and each part's own choice still changes that part alone.
 */
const perPart = computed(() =>
  (props.item.patterns ?? []).filter((p) => props.item.parts?.length && p.parts?.length === 1),
);

/** The durations every one of those patterns offers. */
const sharedDurations = computed(() => {
  const [first, ...rest] = perPart.value.map(durations);
  return (first ?? []).filter((d) => rest.every((other) => other.some((o) => o.value === d.value)));
});

/** What every part you haven't refused is set to, or "mixed" when they differ. */
const everyKeep = computed<Keep | "mixed">(() => {
  const chosen = new Set(perPart.value.filter((p) => !blocked(p)).map((p) => keep.value[p.id] ?? ""));
  if (chosen.size !== 1) return "mixed";
  const [only] = chosen;
  if (only === "until") {
    const times = new Set(perPart.value.filter((p) => !blocked(p)).map((p) => untils.value[p.id] ?? ""));
    if (times.size !== 1) return "mixed";
  }
  return only as Keep;
});

/** The time every part is remembered until, when they share one. */
const everyUntil = computed(() => {
  const first = perPart.value.find((p) => !blocked(p));
  return everyKeep.value === "until" && first ? (untils.value[first.id] ?? "") : "";
});

/** Set how long every part you haven't refused is remembered. */
function keepEvery(value: string): void {
  for (const pattern of perPart.value) {
    if (!blocked(pattern)) keep.value[pattern.id] = value as Keep;
  }
}

function untilEvery(time: string): void {
  for (const pattern of perPart.value) {
    if (!blocked(pattern)) untils.value[pattern.id] = time;
  }
}

const remembering = computed(() =>
  (props.item.patterns ?? []).filter((p) => keep.value[p.id] && !blocked(p)),
);

/** How this decision will be signed, said beside the buttons. */
const signing = computed(() => {
  if (reconsidering.value) {
    if (!preferences.stayUnlocked) return "Each decision is signed with your passkey.";
    if (unlock.unlocked) return "It only takes something back, so this unlocked tab signs it.";
    return "Your passkey signs it, and unlocks deciding in this tab while you use it.";
  }
  if (props.item.risk === "high") {
    return "It's high risk, so your passkey signs it, even while deciding is unlocked.";
  }
  const beyond = props.item.requester.session ? ["until", "permanent"] : ["until", "hours", "permanent"];
  const lastsBeyond =
    props.item.kind === "connect" || remembering.value.some((p) => beyond.includes(keep.value[p.id] ?? ""));
  if (lastsBeyond) {
    return "This decision lasts beyond the run or session, so your passkey signs it, even while deciding is unlocked.";
  }
  if (!preferences.stayUnlocked) return "Each decision is signed with your passkey.";
  if (unlock.unlocked) {
    return `Deciding is unlocked in this tab; it locks after ${minutes(unlock.idleSeconds)} unused.`;
  }
  return "Your passkey signs this decision and unlocks deciding in this tab while you use it.";
});

function togglePart(id: string): void {
  if (refused.value.has(id)) refused.value.delete(id);
  else refused.value.add(id);
}

/** A key's fingerprint, as the decision you sign names it: its SHA-256, in hex. */
async function fingerprint(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return `sha256:${[...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
}

/**
 * Connect the service with the key pasted: the decision names the key's fingerprint
 * and the settings given with it, and the key goes beside the decision, never in it.
 */
async function connect(choice: DecisionChoice): Promise<void> {
  const service = connecting.value;
  const pasted = key.value.trim();
  if (!service || !pasted) {
    problem.value = `Paste the ${service?.label ?? "service's"} key first.`;
    return;
  }
  for (const setting of service.settings) {
    const value = (settings.value[setting.name] ?? "").trim();
    if (!new RegExp(`^(?:${setting.pattern})$`).test(value)) {
      problem.value = `${setting.label} doesn't look like one.`;
      return;
    }
  }
  choice.credential_fingerprint = await fingerprint(pasted);
  choice.credential_settings = Object.fromEntries(
    service.settings.map((s) => [s.name, (settings.value[s.name] ?? "").trim()]),
  );
  key.value = "";
  emit("decide", choice, { [props.item.id]: pasted });
}

/** Build the decision for an action from what's been chosen, or say what's missing. */
async function choose(action: Action): Promise<void> {
  problem.value = "";
  const needs = action.needs ?? [];
  const choice: DecisionChoice = {
    item: props.item.id,
    content_hash: props.item.content_hash,
    action: action.id,
  };
  if (needs.includes("credential")) {
    if (note.value.trim()) choice.note = note.value.trim();
    await connect(choice);
    return;
  }
  if (reconsidering.value && props.item.decision) choice.supersedes = props.item.decision.id;
  if (note.value.trim()) choice.note = note.value.trim();
  if (needs.includes("note") && !choice.note) {
    problem.value = `${action.label} needs a note for the requester.`;
    return;
  }
  if (props.item.kind === "question" && action.id === "answer") {
    if (answerText.value.trim()) choice.answer = { text: answerText.value.trim() };
    else if (answerChoice.value) choice.answer = { choice: answerChoice.value };
    else {
      problem.value = "Choose an answer, or write one.";
      return;
    }
  }
  const approving = APPROVING.has(action.id);
  if (approving && props.item.parts?.length && refused.value.size) {
    choice.parts = Object.fromEntries(
      props.item.parts.map((p) => [p.id, refused.value.has(p.id) ? "deny" : "approve"]),
    );
  }
  if (approving && remembering.value.length) {
    const remember: Remember[] = [];
    for (const pattern of remembering.value) {
      const lasts = keep.value[pattern.id];
      if (lasts === "hours") {
        const until = new Date(Date.now() + MOUNT_HOURS * 3600 * 1000).toISOString();
        const within: Lasts = props.item.requester.session ? "session" : "until";
        remember.push({ pattern: pattern.id, lasts: within, until });
        continue;
      }
      if (lasts !== "until") {
        remember.push({ pattern: pattern.id, lasts: lasts as Lasts });
        continue;
      }
      const time = new Date(untils.value[pattern.id] ?? "");
      if (Number.isNaN(time.getTime()) || time.getTime() <= Date.now()) {
        problem.value = `Choose a time in the future to remember ${pattern.summary} until.`;
        return;
      }
      remember.push({ pattern: pattern.id, lasts: "until", until: time.toISOString() });
    }
    choice.remember = remember;
  }
  emit("decide", choice);
}
</script>

<template>
  <form class="decide" :aria-describedby="`passkey-note-${item.id}`" @submit.prevent>
    <p v-if="hasMounts" class="explain">
      A mount is never approved just once: the agent runs its script again once you
      decide, and only a remembered mount covers it.
      <template v-if="item.requester.session">
        A session lasts until the agent's app closes or reconnects, or the agent goes an
        hour without using maji-mcp.
      </template>
      <template v-else>
        The agent's app named no session, so a mount is kept for a time instead.
      </template>
    </p>
    <p v-if="thisSession && item.requester.session" :id="thisSession" class="this-session">
      This session is <SessionName :session="item.requester.session" :name="item.session_name" quiet-key />.
    </p>
    <fieldset v-if="item.parts?.length">
      <legend>What it asks for. Untick anything you refuse.</legend>
      <div v-if="perPart.length > 1" class="keep keep-every">
        <label :for="`keep-every-${item.id}`" class="keep-label">Remember all</label>
        <select
          :id="`keep-every-${item.id}`"
          :value="everyKeep"
          :aria-describedby="thisSession ?? undefined"
          @change="keepEvery(($event.target as HTMLSelectElement).value)"
        >
          <option v-if="everyKeep === 'mixed'" value="mixed" disabled>Each as chosen below</option>
          <option v-for="d in sharedDurations" :key="d.value" :value="d.value">{{ d.label }}</option>
        </select>
        <template v-if="everyKeep === 'until'">
          <label :for="`until-every-${item.id}`" class="keep-label">Until</label>
          <input
            :id="`until-every-${item.id}`"
            :value="everyUntil"
            type="datetime-local"
            @input="untilEvery(($event.target as HTMLInputElement).value)"
          />
        </template>
      </div>
      <div v-for="part in item.parts" :key="part.id" class="part">
        <div class="option">
          <input
            :id="`part-${part.id}`"
            type="checkbox"
            :checked="!refused.has(part.id)"
            @change="togglePart(part.id)"
          />
          <label :for="`part-${part.id}`"
            ><template v-for="(piece, at) in afterSlashes(part.label)" :key="at"
              ><wbr v-if="at" />{{ piece }}</template
            ></label
          >
        </div>
        <div v-for="p in patternsFor(part.id)" :key="p.id" class="keep">
          <label :for="`keep-${p.id}`" class="keep-label">
            Remember <span class="visually-hidden">{{ p.summary }}</span>
          </label>
          <select
            :id="`keep-${p.id}`"
            v-model="keep[p.id]"
            :disabled="refused.has(part.id)"
            :aria-describedby="thisSession ?? undefined"
          >
            <option v-for="d in durations(p)" :key="d.value" :value="d.value">{{ d.label }}</option>
          </select>
          <template v-if="keep[p.id] === 'until' && !refused.has(part.id)">
            <label :for="`until-${p.id}`" class="keep-label">Until</label>
            <input :id="`until-${p.id}`" v-model="untils[p.id]" type="datetime-local" />
          </template>
        </div>
      </div>
    </fieldset>

    <fieldset v-if="item.kind === 'question'">
      <legend>Your answer</legend>
      <div v-for="option in item.choices ?? []" :key="option.id" class="option">
        <input :id="`answer-${option.id}`" v-model="answerChoice" type="radio" :value="option.id" name="answer" />
        <label :for="`answer-${option.id}`">{{ option.label }}</label>
      </div>
      <label :for="`answer-text-${item.id}`" class="field-label">
        {{ item.choices?.length ? "Or write your own" : "Your answer" }}
      </label>
      <textarea :id="`answer-text-${item.id}`" v-model="answerText" rows="2" maxlength="4000"></textarea>
    </fieldset>

    <fieldset v-if="broader.length">
      <legend>{{ item.parts?.length ? "Remember more broadly?" : "Remember this approval?" }}</legend>
      <div v-for="p in broader" :key="p.id" class="part">
        <p class="pattern">
          {{ p.summary }}
          <span v-if="p.would_have_matched?.length" class="hint">
            It would have covered {{ p.would_have_matched.length }} recent
            {{ p.would_have_matched.length === 1 ? "request" : "requests" }}.
          </span>
          <span v-if="blocked(p)" class="hint">It covers a part you refused.</span>
        </p>
        <div class="keep">
          <label :for="`keep-${p.id}`" class="keep-label">
            Remember <span class="visually-hidden">{{ p.summary }}</span>
          </label>
          <select
            :id="`keep-${p.id}`"
            v-model="keep[p.id]"
            :disabled="blocked(p)"
            :aria-describedby="thisSession ?? undefined"
          >
            <option v-for="d in durations(p)" :key="d.value" :value="d.value">{{ d.label }}</option>
          </select>
          <template v-if="keep[p.id] === 'until' && !blocked(p)">
            <label :for="`until-${p.id}`" class="keep-label">Until</label>
            <input :id="`until-${p.id}`" v-model="untils[p.id]" type="datetime-local" />
          </template>
        </div>
      </div>
    </fieldset>

    <div class="note">
      <label :for="`note-${item.id}`" class="field-label">{{ noteLabel }}</label>
      <textarea :id="`note-${item.id}`" v-model="note" rows="2" maxlength="4000"></textarea>
    </div>

    <fieldset v-if="connecting">
      <legend>Your {{ connecting.label }} key</legend>
      <p class="explain">
        Make a key that can do only this: {{ connecting.permissions.join(", ") }}.
        <a v-if="connecting.key_page" :href="connecting.key_page" target="_blank" rel="noopener noreferrer"
          >Make one at {{ connecting.host }}</a
        >. Only the broker keeps the key, and sends it only to {{ connecting.host }}; the
        queue records its fingerprint.
      </p>
      <label :for="`key-${item.id}`" class="field-label">{{ connecting.label }} key</label>
      <input
        :id="`key-${item.id}`"
        v-model="key"
        type="password"
        autocomplete="off"
        autocapitalize="off"
        spellcheck="false"
      />
      <template v-for="s in connecting.settings" :key="s.name">
        <label :for="`setting-${item.id}-${s.name}`" class="field-label">{{ s.label }}</label>
        <input
          :id="`setting-${item.id}-${s.name}`"
          v-model="settings[s.name]"
          type="text"
          autocomplete="off"
          autocapitalize="off"
          spellcheck="false"
          :aria-describedby="s.description ? `setting-${item.id}-${s.name}-hint` : undefined"
        />
        <p v-if="s.description" :id="`setting-${item.id}-${s.name}-hint`" class="hint">
          {{ s.description }}
        </p>
      </template>
    </fieldset>

    <p v-if="problem" class="problem" role="alert">{{ problem }}</p>

    <div class="actions">
      <button
        v-for="action in actions"
        :key="action.id"
        type="button"
        :class="action.style ?? 'neutral'"
        :disabled="busy"
        @click="choose(action)"
      >
        {{ action.label }}
      </button>
    </div>
    <p :id="`passkey-note-${item.id}`" class="hint">{{ signing }}</p>
  </form>
</template>

<style scoped src="./DecisionForm.css"></style>
