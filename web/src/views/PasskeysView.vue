<script setup lang="ts">
import { onMounted, ref } from "vue";

import { ApiError } from "@/api/client";
import type { Passkey } from "@/api/client";
import { when } from "@/lib/describe";
import { useAnnouncer } from "@/stores/announcer";
import { usePasskeys } from "@/stores/passkeys";
import { usePreferences } from "@/stores/preferences";
import { minutes, useUnlock } from "@/stores/unlock";

const passkeys = usePasskeys();
const preferences = usePreferences();
const unlock = useUnlock();
const announcer = useAnnouncer();
const name = ref("");
const busy = ref(false);

onMounted(async () => {
  try {
    await passkeys.load();
  } catch (error) {
    announcer.warn(`The passkeys couldn't be loaded: ${(error as Error).message}.`);
  }
});

async function add(): Promise<void> {
  busy.value = true;
  try {
    if (await passkeys.add(name.value.trim() || "Another device")) {
      announcer.announce(`Added ${name.value.trim() || "a passkey"}.`);
      name.value = "";
    } else {
      announcer.announce("No passkey was added.");
    }
  } catch (error) {
    announcer.warn(`The passkey wasn't added: ${(error as Error).message}.`);
  } finally {
    busy.value = false;
  }
}

async function stayUnlocked(on: boolean): Promise<void> {
  preferences.stayUnlocked = on;
  if (!on && unlock.unlocked) {
    await unlock.lock();
    announcer.announce("Deciding is locked; each decision will ask for your passkey.");
  }
}

async function remove(passkey: Passkey): Promise<void> {
  busy.value = true;
  try {
    if (await passkeys.remove(passkey.id)) announcer.announce(`Removed ${passkey.name}.`);
  } catch (error) {
    announcer.warn(
      error instanceof ApiError && error.status === 409
        ? "That is the last passkey, so it stays: without one, nothing could be decided."
        : `The passkey wasn't removed: ${(error as Error).message}.`,
    );
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="passkeys">
    <h1 data-page-heading tabindex="-1">Passkeys</h1>
    <p class="intro">
      These are the passkeys whose signatures this control plane accepts. Check that you
      recognize every one. Adding or removing one asks you to sign in again if you last
      did more than five minutes ago.
    </p>

    <section aria-labelledby="list-title">
      <h2 id="list-title">Registered</h2>
      <ul class="list">
        <li v-for="passkey in passkeys.passkeys" :key="passkey.id">
          <div>
            <p class="name">{{ passkey.name }}</p>
            <p class="meta">
              Added <time :datetime="passkey.created_at">{{ when(passkey.created_at) }}</time>
              <template v-if="passkey.last_used_at">
                · last used <time :datetime="passkey.last_used_at">{{ when(passkey.last_used_at) }}</time>
              </template>
            </p>
          </div>
          <button
            type="button"
            class="danger"
            :disabled="busy"
            :aria-label="`Remove the passkey ${passkey.name}`"
            @click="remove(passkey)"
          >
            Remove
          </button>
        </li>
      </ul>
    </section>

    <section aria-labelledby="deciding-title">
      <h2 id="deciding-title">Deciding</h2>
      <label class="toggle">
        <input
          type="checkbox"
          :checked="preferences.stayUnlocked"
          aria-describedby="deciding-detail"
          @change="stayUnlocked(($event.target as HTMLInputElement).checked)"
        />
        Keep deciding unlocked in a tab while I'm using it
      </label>
      <p id="deciding-detail" class="detail">
        Your passkey unlocks deciding in the tab you decide in, and the tab locks again
        once you haven't used it for {{ unlock.unlocked ? minutes(unlock.idleSeconds) : "a while" }}.
        Meanwhile, anything able to run in that tab could decide too. Decisions that last
        beyond their run or session, and connecting a service, always ask for the passkey.
        Turned off, every decision asks for it.
      </p>
    </section>

    <section aria-labelledby="add-title">
      <h2 id="add-title">Add a passkey on this device</h2>
      <form class="add" @submit.prevent="add">
        <label for="passkey-name">Name</label>
        <input id="passkey-name" v-model="name" maxlength="100" placeholder="such as Work laptop" />
        <button type="submit" :disabled="busy">Add a passkey</button>
      </form>
    </section>

    <section aria-labelledby="recovery-title">
      <h2 id="recovery-title">Recovery codes</h2>
      <p>
        The ten recovery codes shown when you set up your first passkey each add a passkey
        once, from the sign-in page, if you lose yours. Keep them in your password manager.
      </p>
    </section>
  </div>
</template>

<style scoped src="./PasskeysView.css"></style>
