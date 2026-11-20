<script setup lang="ts">
import { onMounted, ref, watch } from "vue";

import { ApiError } from "@/api/client";
import type { Grant } from "@/api/client";
import SessionName from "@/components/SessionName.vue";
import { when } from "@/lib/describe";
import { segments } from "@/lib/highlight";
import { useAnnouncer } from "@/stores/announcer";
import { type GroupBy, NONE, useGrants } from "@/stores/grants";

const grants = useGrants();
const announcer = useAnnouncer();
const confirming = ref<string | null>(null);
let typing: ReturnType<typeof setTimeout> | undefined;

const BY: { value: GroupBy; label: string }[] = [
  { value: "resource", label: "What it covers" },
  { value: "principal", label: "Who holds it" },
  { value: "access", label: "Kind of access" },
  { value: "lasts", label: "How long it lasts" },
  { value: "session", label: "Which session holds it" },
];
const LASTS: Record<string, string> = {
  run: "for one run",
  session: "for a session",
  until: "until a time",
  permanent: "until revoked",
};

async function reload(): Promise<void> {
  try {
    await grants.load();
  } catch (error) {
    announcer.warn(`The grants couldn't be loaded: ${(error as Error).message}.`);
  }
}

onMounted(reload);
watch(
  () => grants.by,
  () => {
    grants.group = null;
    confirming.value = null;
    void reload();
  },
);
watch(
  () => grants.query,
  () => {
    clearTimeout(typing);
    typing = setTimeout(async () => {
      await reload();
      if (grants.query.trim()) {
        const n = grants.grants.length;
        announcer.announce(`${n} ${n === 1 ? "grant matches" : "grants match"}.`);
      }
    }, 250);
  },
);

function show(key: string | null): void {
  grants.group = grants.group === key ? null : key;
  confirming.value = null;
  void reload();
}

/**
 * A group, in words: a session by its name, the group without a value as such, and
 * anything else by what it shares.
 */
function named(key: string): string {
  if (grants.by === "session") {
    const name = grants.groups.find((g) => g.key === key)?.session_name;
    return key === NONE ? "no session" : `session ${name ?? key}`;
  }
  return key;
}

/** The field a search matched, split into the matched characters and the rest. */
function marked(grant: Grant, field: "summary" | "resource" | "principal", text: string) {
  return grant.match?.field === field ? segments(text, grant.match.ranges) : [{ text, matched: false }];
}

async function revokeOne(grant: Grant): Promise<void> {
  try {
    await grants.revoke([grant.id]);
    announcer.announce(`Revoked: ${grant.pattern.summary}.`);
  } catch (error) {
    announcer.warn(error instanceof ApiError ? error.message : "The grant wasn't revoked.");
  }
}

async function revokeGroup(key: string): Promise<void> {
  // Named first: once revoked, the group, and the name it carries, is gone.
  //
  const label = named(key);
  try {
    const n = await grants.revokeGroup(key);
    confirming.value = null;
    announcer.announce(`Revoked ${n} ${n === 1 ? "grant" : "grants"} for ${label}.`);
  } catch (error) {
    announcer.warn(error instanceof ApiError ? error.message : "Nothing was revoked.");
  }
}
</script>

<template>
  <div class="grants">
    <h1 data-page-heading tabindex="-1">Grants</h1>
    <p class="intro">
      What agents may do without asking you again. Revoking takes effect at once, and
      needs no passkey.
    </p>

    <div class="controls">
      <div class="field">
        <label for="grant-search">Find a grant</label>
        <input
          id="grant-search"
          v-model="grants.query"
          type="search"
          autocomplete="off"
          aria-describedby="grant-search-hint"
        />
        <p id="grant-search-hint" class="hint">
          Type fragments of a repository, a service or who holds it, in any order.
        </p>
      </div>
      <div class="field">
        <label for="grant-by">Group by</label>
        <select id="grant-by" v-model="grants.by">
          <option v-for="option in BY" :key="option.value" :value="option.value">{{ option.label }}</option>
        </select>
      </div>
    </div>

    <section aria-labelledby="groups-title">
      <h2 id="groups-title">Groups</h2>
      <p v-if="!grants.groups.length" class="empty">No grants.</p>
      <ul v-else class="groups">
        <li v-for="g in grants.groups" :key="g.key">
          <button
            type="button"
            class="group"
            :aria-pressed="grants.group === g.key"
            @click="show(g.key)"
          >
            <span class="key" :class="{ words: grants.by === 'session' }">
              <SessionName v-if="grants.by === 'session' && g.key !== NONE" :session="g.key" :name="g.session_name" />
              <template v-else-if="grants.by === 'session'">Not held for a session</template>
              <template v-else>{{ g.key }}</template>
            </span>
            <span class="count">{{ g.count }} {{ g.count === 1 ? "grant" : "grants" }}<template v-if="g.permanent">, {{ g.permanent }} until revoked</template></span>
          </button>
          <!-- Grants without a value can't be revoked by a filter, so only one by one. -->
          <div v-if="g.key !== NONE" class="revoke-group">
            <button v-if="confirming !== g.key" type="button" class="danger" @click="confirming = g.key">
              Revoke all {{ g.count }}
            </button>
            <template v-else>
              <span :id="`confirm-${g.key}`">Revoke all {{ g.count }} for {{ named(g.key) }}?</span>
              <button type="button" class="danger" :aria-describedby="`confirm-${g.key}`" @click="revokeGroup(g.key)">
                Revoke them
              </button>
              <button type="button" class="neutral" @click="confirming = null">Keep them</button>
            </template>
          </div>
        </li>
      </ul>
    </section>

    <section aria-labelledby="list-title">
      <h2 id="list-title">
        {{ grants.query.trim() ? "Matching grants" : grants.group ? `Grants: ${named(grants.group)}` : "All grants" }}
      </h2>
      <p v-if="!grants.grants.length" class="empty">Nothing matches.</p>
      <ul v-else class="list">
        <li v-for="grant in grants.grants" :key="grant.id">
          <div class="what">
            <p class="summary">
              <template v-for="(part, i) in marked(grant, 'summary', grant.pattern.summary)" :key="i">
                <mark v-if="part.matched">{{ part.text }}</mark><template v-else>{{ part.text }}</template>
              </template>
            </p>
            <p class="meta">
              <span>
                Held by
                <template v-for="(part, i) in marked(grant, 'principal', grant.principal)" :key="i">
                  <mark v-if="part.matched">{{ part.text }}</mark><template v-else>{{ part.text }}</template>
                </template>
              </span>
              <span v-if="grant.pattern.resource">
                · covers
                <code><template v-for="(part, i) in marked(grant, 'resource', grant.pattern.resource)" :key="i"><mark v-if="part.matched">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></code>
              </span>
              <span>
                ·
                <template v-if="grant.lasts === 'session' && grant.session && grant.session_name"
                  >for session <SessionName :session="grant.session" :name="grant.session_name" /></template
                ><template v-else>{{ LASTS[grant.lasts] ?? grant.lasts }}</template
                ><template v-if="grant.until"> (<time :datetime="grant.until">{{ when(grant.until) }}</time>)</template></span
              >
              <span> · granted <time :datetime="grant.granted_at">{{ when(grant.granted_at) }}</time></span>
            </p>
          </div>
          <button
            type="button"
            class="danger"
            :aria-label="`Revoke ${grant.pattern.summary}`"
            @click="revokeOne(grant)"
          >
            Revoke
          </button>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped src="./GrantsView.css"></style>
