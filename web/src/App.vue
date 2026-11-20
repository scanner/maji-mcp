<script setup lang="ts">
import { ref } from "vue";
import { RouterLink, RouterView, useRouter } from "vue-router";

import KeyboardHelp from "@/components/KeyboardHelp.vue";
import StatusRegion from "@/components/StatusRegion.vue";
import TagHelp from "@/components/TagHelp.vue";
import { useSession } from "@/stores/session";
import { useUnlock } from "@/stores/unlock";

const session = useSession();
const unlock = useUnlock();
const router = useRouter();
const help = ref<InstanceType<typeof KeyboardHelp> | null>(null);
const tags = ref<InstanceType<typeof TagHelp> | null>(null);

async function signOut(): Promise<void> {
  unlock.forget();
  await session.signOut();
  await router.push({ name: "sign-in" });
}
</script>

<template>
  <a class="skip" href="#main">Skip to the main content</a>
  <header class="masthead">
    <p class="brand">maji-mcp</p>
    <nav v-if="session.state === 'signed-in'" aria-label="Main">
      <ul>
        <li><RouterLink to="/queue">Queue</RouterLink></li>
        <li><RouterLink to="/grants">Grants</RouterLink></li>
        <li><RouterLink to="/connections">Connections</RouterLink></li>
        <li><RouterLink to="/passkeys">Passkeys</RouterLink></li>
      </ul>
    </nav>
    <div v-if="session.state === 'signed-in'" class="tools">
      <template v-if="unlock.unlocked">
        <span class="unlocked" :class="{ ending: unlock.ending }">
          Deciding unlocked · {{ unlock.left }}
        </span>
        <button type="button" class="quiet" @click="unlock.lock()">Lock</button>
      </template>
      <button type="button" class="quiet" @click="tags?.open()">Tags</button>
      <button type="button" class="quiet" @click="help?.open()">Keyboard</button>
      <button type="button" class="quiet" @click="signOut">Sign out</button>
    </div>
  </header>
  <main id="main" tabindex="-1">
    <RouterView />
  </main>
  <KeyboardHelp ref="help" />
  <TagHelp ref="tags" />
  <StatusRegion />
</template>

<style scoped src="./App.css"></style>
