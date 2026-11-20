/**
 * Whether this browser is signed in, and signing in and out with a passkey.
 */
import { defineStore } from "pinia";
import { ref } from "vue";

import { ApiError, api, done, ok } from "@/api/client";
import { sign } from "@/api/webauthn";

export type SessionState = "unknown" | "signed-in" | "signed-out";

export const useSession = defineStore("session", () => {
  const state = ref<SessionState>("unknown");

  /** Ask the control plane whether the session cookie is still good. */
  async function check(): Promise<SessionState> {
    const result = await api.GET("/passkeys");
    state.value = result.response.ok ? "signed-in" : "signed-out";
    return state.value;
  }

  /**
   * Sign in with a passkey.
   *
   * Returns:
   *     True once signed in; false if the person cancelled the passkey prompt
   */
  async function signIn(): Promise<boolean> {
    const options = ok(await api.POST("/session/options"));
    const assertion = await sign(options.public_key);
    if (assertion === null) return false;
    done(
      await api.POST("/session", {
        body: { challenge_id: options.challenge_id, assertion: assertion as never },
      }),
    );
    state.value = "signed-in";
    return true;
  }

  async function signOut(): Promise<void> {
    try {
      done(await api.DELETE("/session"));
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) throw error;
    }
    state.value = "signed-out";
  }

  return { state, check, signIn, signOut };
});
