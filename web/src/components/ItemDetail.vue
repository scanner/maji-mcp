<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { DecisionChoice, Item, Pasted } from "@/api/client";
import DecisionForm from "@/components/DecisionForm.vue";
import DiffView from "@/components/DiffView.vue";
import FactList from "@/components/FactList.vue";
import SessionName from "@/components/SessionName.vue";
import {
  DECIDED,
  RISK_REASONS,
  WAITING_TAG,
  decided,
  evidence,
  kindLabel,
  refusedMounts,
  riskTag,
  stateLabel,
  waitedOn,
  when,
  isNewFile,
} from "@/lib/describe";
import { aloneBecause } from "@/stores/queue";

const props = defineProps<{
  item: Item;
  busy: boolean;
  /**
   * Whether the item is shown in its row of a list, beneath the row's button, which
   * already shows its title, subject and tags.
   */
  inline?: boolean;
}>();
const emit = defineEmits<{
  decide: [choice: DecisionChoice, pasted?: Pasted];
  askAgain: [id: string];
}>();
const showWords = ref(false);
const record = computed(() => decided(props.item));

// In a list, the item sits under the list's heading, so its own headings are a level
// lower. Its title stays a heading for screen readers and for focus after a decision,
// hidden from view, since the row's button above it shows the title.
//
const heading = computed(() => (props.inline ? "h3" : "h2"));
const sub = computed(() => (props.inline ? "h4" : "h3"));
const subsub = computed(() => (props.inline ? "h5" : "h4"));
const refusedPaths = computed(() => refusedMounts(props.item));

/** What the requester's app reported itself as when it connected, unchecked. */
const client = computed(() => {
  const said = props.item.requester.client;
  return said ? [said.name, said.version].filter(Boolean).join(" ") : null;
});

/**
 * A question's title is the question itself, in the requester's words, so it is
 * labelled as theirs, and its claim is why it's asked. Any other item's claim is the
 * requester's account of the request, and comes after the change and the facts, so
 * it never frames them before you've seen them.
 */
const question = computed(() => props.item.kind === "question");

/** The answer a question was given: its choice's label, or the words typed. */
const answer = computed(() => {
  const given = props.item.decision?.answer;
  if (!given) return null;
  const choice = props.item.choices?.find((c) => c.id === given.choice);
  return choice?.label ?? given.text ?? given.choice ?? null;
});

watch(
  () => props.item.id,
  () => {
    showWords.value = false;
  },
);

/** What reconsidering each kind of decision does, said above its form. */
const RECONSIDERING: Record<string, { title: string; explain: string }> = {
  demote: {
    title: "Demote",
    explain:
      "Demoting stops every agent running this version, the agent that kept it too. " +
      "An agent that tries it is told you demoted it, with your note. It can keep a " +
      "changed version, which runs as its own until you promote one.",
  },
};
const reconsidering = computed(() =>
  props.item.state === "waiting" ? undefined : RECONSIDERING[props.item.actions[0]?.id ?? ""],
);
</script>

<template>
  <article class="detail" :class="{ inline }" :aria-labelledby="`title-${item.id}`">
    <header>
      <component
        :is="heading"
        :id="`title-${item.id}`"
        class="heading"
        :class="{ 'visually-hidden': inline }"
        data-item-heading
        tabindex="-1"
        >{{ item.title }}</component
      >
      <template v-if="!inline">
        <p v-if="item.subject" class="subject"><code>{{ item.subject }}</code></p>
        <p class="tags">
          <span class="tag">{{ kindLabel(item) }}</span>
          <span v-if="riskTag(item)" class="tag risk-high">{{ riskTag(item) }}</span>
          <span class="tag">{{ stateLabel(item) }}</span>
          <span v-if="waitedOn(item)" class="tag">{{ WAITING_TAG }}</span>
        </p>
      </template>
      <p class="from">
        From <strong>{{ item.requester.principal }}</strong>
        <template v-if="client"> ({{ client }})</template>
        <!-- Open in its row, the row's line just above shows the session, so here it is
             left to screen readers, which hear it, key and all, where the item says who
             asked; the row reads only the name. -->
        <span v-if="item.requester.session" :class="{ 'visually-hidden': inline }">
          in session <SessionName :session="item.requester.session" :name="item.session_name" />
        </span>
        <template v-if="item.requester.machine"> on {{ item.requester.machine }}</template>,
        <time :datetime="item.created_at">{{ when(item.created_at) }}</time>.
        <template v-if="item.expires_at">
          Lapses <time :datetime="item.expires_at">{{ when(item.expires_at) }}</time>.
        </template>
      </p>
      <p v-if="question" class="caveat">Asked in the requester's own words, not checked: it may repeat text it read.</p>
    </header>

    <section v-if="item.risk_reasons?.length" :aria-labelledby="`risk-${item.id}`" class="risk">
      <component :is="sub" class="sub" :id="`risk-${item.id}`">Why it's high risk</component>
      <ul>
        <li v-for="(reason, index) in item.risk_reasons" :key="index">
          <strong
            >{{ RISK_REASONS[reason.code] ?? reason.code
            }}<template v-if="reason.basis === 'suspected'"> (suspected)</template>:</strong
          >
          {{ reason.text }}
          <ul v-if="evidence(item, reason).length" class="evidence">
            <li v-for="(shown, at) in evidence(item, reason)" :key="at">
              {{ shown.said }}: <code v-if="shown.line">{{ shown.text }}</code
              ><template v-else>{{ shown.text }}</template>
            </li>
          </ul>
        </li>
      </ul>
    </section>

    <section v-if="item.diff" :aria-labelledby="`diff-${item.id}`">
      <component :is="sub" class="sub" :id="`diff-${item.id}`">{{
        item.kind === "promote" && isNewFile(item.diff) ? "The code" : "The change"
      }}</component>
      <DiffView :diff="item.diff" />
    </section>

    <section v-if="item.facts.length" :aria-labelledby="`facts-${item.id}`" class="found">
      <component :is="sub" class="sub" :id="`facts-${item.id}`">What the broker found</component>
      <FactList :facts="item.facts" />
    </section>

    <section v-if="item.claim" :aria-labelledby="`claim-${item.id}`" class="claim">
      <component :is="sub" class="sub" :id="`claim-${item.id}`">{{ question ? "Why it's asked" : "What the requester says" }}</component>
      <p v-if="!question" class="caveat">Written by the requester and not checked: it may repeat text it read.</p>
      <blockquote>{{ item.claim }}</blockquote>
    </section>

    <section v-if="item.words" class="words">
      <button
        type="button"
        class="disclose"
        :aria-expanded="showWords"
        :aria-controls="`words-${item.id}`"
        @click="showWords = !showWords"
      >
        {{ showWords ? "Hide" : "Show" }} what you typed
      </button>
      <blockquote v-show="showWords" :id="`words-${item.id}`">{{ item.words }}</blockquote>
    </section>

    <section v-if="item.notes?.length" :aria-labelledby="`notes-${item.id}`">
      <component :is="sub" class="sub" :id="`notes-${item.id}`">Notes</component>
      <ul class="notes">
        <li v-for="(note, index) in item.notes" :key="index">
          <strong>{{ note.author === "advisor" ? "The advisor" : "You" }}:</strong> {{ note.text }}
        </li>
      </ul>
    </section>

    <section v-if="item.state === 'waiting'" :aria-labelledby="`decide-${item.id}`">
      <component :is="sub" class="sub" :id="`decide-${item.id}`">Decide</component>
      <p v-if="aloneBecause(item)" class="alone">
        Decide this on its own, not in a batch: {{ aloneBecause(item) }}.
      </p>
      <DecisionForm :item="item" :busy="busy" @decide="(...decided) => emit('decide', ...decided)" />
    </section>

    <section v-else-if="item.decision" :aria-labelledby="`decided-${item.id}`" class="record">
      <component :is="sub" class="sub" :id="`decided-${item.id}`">Decision</component>
      <p>
        {{ DECIDED[item.decision.action] ?? item.decision.action }}
        <template v-if="item.decision.by.kind === 'you'">by you</template>
        <template v-else-if="item.decision.by.rule">by the rule “{{ item.decision.by.rule }}”</template>
        <template v-else>by policy</template>,
        <time :datetime="item.decision.at">{{ when(item.decision.at) }}</time>.
        <template v-if="item.decision.by.unlock">Signed in a tab your passkey unlocked.</template>
        <template v-else-if="item.decision.signature">Signed with your passkey.</template>
      </p>
      <p v-if="item.superseded">
        It takes back your earlier decision:
        {{ DECIDED[item.superseded.action] ?? item.superseded.action }},
        <time :datetime="item.superseded.at">{{ when(item.superseded.at) }}</time>.
      </p>
      <p v-if="answer !== null">Your answer: {{ answer }}</p>
      <ul v-if="record.parts.length" class="parts" aria-label="What it asked for">
        <li v-for="part in record.parts" :key="part.id">
          <strong>{{ part.label }}</strong
          ><template v-if="part.refused !== null">: {{ part.refused ? "refused" : "approved" }}</template
          ><template v-for="(lasts, index) in part.kept" :key="index">, remembered {{ lasts }}</template>.
        </li>
      </ul>
      <p v-for="(kept, index) in record.broader" :key="index">
        Remembered: {{ kept.summary }}, {{ kept.lasts }}.
      </p>
      <template v-if="refusedPaths.length">
        <p v-if="item.ask_again">
          You let the agent ask again for what you refused,
          <time :datetime="item.ask_again.at">{{ when(item.ask_again.at) }}</time>. Its next
          request for it comes to you as a new item.
        </p>
        <template v-else>
          <p>
            The agent can't ask again for what you refused here, so it can't keep putting
            it to you. If you've told it more since, you can let it ask again.
          </p>
          <button type="button" class="ask-again" :disabled="busy" @click="emit('askAgain', item.id)">
            Let it ask again
          </button>
        </template>
      </template>
      <p v-if="item.decision.note">Your note: {{ item.decision.note }}</p>
      <p v-if="item.decision.applied">
        Applied: {{ item.decision.applied.outcome }}<template v-if="item.decision.applied.detail">,
          {{ item.decision.applied.detail }}</template>.
      </p>
      <p v-else>Not applied yet.</p>
      <section v-if="reconsidering" :aria-labelledby="`reconsider-${item.id}`" class="reconsider">
        <component :is="subsub" class="sub" :id="`reconsider-${item.id}`">{{ reconsidering.title }}</component>
        <p>{{ reconsidering.explain }}</p>
        <DecisionForm :item="item" :busy="busy" @decide="(...decided) => emit('decide', ...decided)" />
      </section>
    </section>
  </article>
</template>

<style scoped src="./ItemDetail.css"></style>
