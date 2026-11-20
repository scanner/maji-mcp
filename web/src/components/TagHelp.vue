<script setup lang="ts">
import { ref } from "vue";

import SessionName from "@/components/SessionName.vue";
import { ALONE_TAG, KINDS, RISK_REASONS, STATES, WAITING_TAG, riskTag } from "@/lib/describe";

const dialog = ref<HTMLDialogElement | null>(null);

function open(): void {
  dialog.value?.showModal();
}

defineExpose({ open });

const HIGH = riskTag({ risk: "high" });

/** A made-up session key, for the example of how a session is shown. */
const SAMPLE_KEY = "3f9a61c2" + "0".repeat(56);

/** What each state means, keyed as `STATES` is. */
const STANDING: Record<string, string> = {
  waiting: "it needs your decision",
  decided: "you or a rule decided it, and its details say how",
  expired: "nobody decided it in time, so it went back to its requester",
  withdrawn: "its requester took it back",
};
</script>

<template>
  <dialog ref="dialog" aria-labelledby="tags-title" class="help">
    <!-- Focus starts on the heading: Close is the only control, at the end, and a
         modal dialog would otherwise open scrolled down to it. -->
    <h2 id="tags-title" tabindex="-1" autofocus>What the tags mean</h2>
    <dl>
      <dt><span class="tag">{{ ALONE_TAG }}</span></dt>
      <dd>
        It can't join a batch. A batch approves everything each item asks for, just this
        once, so an item is decided by itself when it has parts you might refuse one by one,
        an approval you might choose to remember, or high risk. Its details say which.
      </dd>
      <dt><span class="tag risk-high">{{ HIGH }}</span></dt>
      <dd>
        The broker found, or suspects, one of five things:
        <ul>
          <li v-for="label in RISK_REASONS" :key="label">{{ label }}</li>
        </ul>
        The item says which, and what it rests on, and your passkey signs the decision. An
        item without the tag isn't known to be safe: none of the five was found.
      </dd>
      <dt><span class="tag blocking">{{ WAITING_TAG }}</span></dt>
      <dd>An agent has stopped until you decide.</dd>
      <dt><span class="tag">{{ KINDS.changeset }}</span>, <span class="tag">{{ KINDS.question }}</span> and the like</dt>
      <dd>What kind of request it is, always the first tag.</dd>
      <dt>Where it stands</dt>
      <dd>
        <ul class="standing">
          <li v-for="(label, state) in STATES" :key="state">
            <span class="tag">{{ label }}</span>: {{ STANDING[state] }}
          </li>
        </ul>
      </dd>
    </dl>
    <h3>In an item's details</h3>
    <dl>
      <dt>From <strong>claude-code</strong> (claude-code 2.1.322)</dt>
      <dd>
        Who asked, in bold: the token the request came with, which is checked. In brackets,
        what the agent's app said it is when it connected, which nothing checks and nothing
        is decided by.
      </dd>
      <dt>In session <SessionName :session="SAMPLE_KEY" name="laughing-sparky-daisy" /></dt>
      <dd>
        The agent's session, named in three words made from its key, so two sessions can be
        told apart. The gateway's log gives it the same name. The 8 characters after it are
        the start of the key, which is what a grant for the session is held to. A session
        that ends and begins again, as when the agent's app reconnects, has a new name.
      </dd>
      <dt><strong class="flag">Worth a look</strong></dt>
      <dd>The broker flagged this fact, or this line of the change.</dd>
      <dt><strong>(suspected)</strong></dt>
      <dd>
        On a reason for high risk: it rests on a guess, a detector's or a judgment of the
        session's goal, and may be wrong. A reason without it was found from what the
        request does.
      </dd>
    </dl>
    <form method="dialog">
      <button type="submit">Close</button>
    </form>
  </dialog>
</template>

<style scoped src="./HelpDialog.css"></style>
<style scoped src="./TagHelp.css"></style>
