<script setup lang="ts">
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { ok, api } from "@/api/client";
import { create } from "@/api/webauthn";
import { useSession } from "@/stores/session";

const session = useSession();
const router = useRouter();
const route = useRoute();
const busy = ref(false);
const problem = ref("");
const recovering = ref(false);
const recoveryCode = ref("");
const deviceName = ref("");

/** Add a passkey with a recovery code, then sign in with it. */
async function recover(): Promise<void> {
  busy.value = true;
  problem.value = "";
  try {
    const name = deviceName.value.trim() || "Recovered passkey";
    const options = ok(await api.POST("/passkeys/registration/options", { body: { name } }));
    const credential = await create(options.public_key);
    if (credential === null) {
      problem.value = "You closed the passkey prompt. Try again when you're ready.";
      return;
    }
    ok(
      await api.POST("/passkeys", {
        body: {
          registration_id: options.registration_id,
          credential: credential as never,
          recovery_code: recoveryCode.value.trim(),
        },
      }),
    );
    recovering.value = false;
    recoveryCode.value = "";
    await signIn();
  } catch (error) {
    problem.value = `The recovery code didn't work: ${(error as Error).message}.`;
  } finally {
    busy.value = false;
  }
}

async function signIn(): Promise<void> {
  busy.value = true;
  problem.value = "";
  try {
    if (await session.signIn()) {
      const next = typeof route.query.next === "string" ? route.query.next : "/queue";
      // Only a path on this site: "//host" would be another site.
      const local = next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\");
      await router.replace(local ? next : "/queue");
    } else {
      problem.value = "You closed the passkey prompt. Sign in when you're ready.";
    }
  } catch (error) {
    problem.value = `Signing in didn't work: ${(error as Error).message}.`;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section class="sign-in" aria-labelledby="sign-in-title">
    <h1 id="sign-in-title" data-page-heading tabindex="-1">Sign in</h1>
    <p>
      Decisions in maji-mcp are signed with your passkey. Sign in with it to see what's waiting
      for you.
    </p>
    <button type="button" :disabled="busy" @click="signIn">
      {{ busy ? "Waiting for your passkey…" : "Sign in with a passkey" }}
    </button>
    <p v-if="problem" class="problem" role="alert">{{ problem }}</p>

    <button
      type="button"
      class="disclose"
      :aria-expanded="recovering"
      aria-controls="recovery"
      @click="recovering = !recovering"
    >
      Lost your passkey? Use a recovery code
    </button>
    <form v-show="recovering" id="recovery" class="recovery" @submit.prevent="recover">
      <label for="recovery-code">Recovery code</label>
      <input id="recovery-code" v-model="recoveryCode" autocomplete="off" spellcheck="false" required />
      <label for="device-name">Name for the new passkey</label>
      <input id="device-name" v-model="deviceName" maxlength="100" placeholder="such as Phone" />
      <button type="submit" :disabled="busy">Add a passkey and sign in</button>
      <p class="hint">Each recovery code works once.</p>
    </form>
  </section>
</template>

<style scoped src="./SignInView.css"></style>
