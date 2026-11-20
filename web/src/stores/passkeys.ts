/**
 * The passkeys this control plane accepts. Adding or removing one needs a sign-in from
 * the last five minutes; the store asks for one when the control plane says so.
 */
import { defineStore } from "pinia";
import { ref } from "vue";

import { ApiError, api, done, ok } from "@/api/client";
import type { Passkey } from "@/api/client";
import { create } from "@/api/webauthn";
import { useSession } from "@/stores/session";

export const usePasskeys = defineStore("passkeys", () => {
  const session = useSession();
  const passkeys = ref<Passkey[]>([]);

  async function load(): Promise<void> {
    passkeys.value = ok(await api.GET("/passkeys")).passkeys;
  }

  /** Run a change that needs a fresh sign-in, signing in again once if asked to. */
  async function fresh<T>(change: () => Promise<T>): Promise<T | null> {
    try {
      return await change();
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 403)) throw error;
      if (!(await session.signIn())) return null;
      return change();
    }
  }

  /**
   * Make a passkey on this device and register it.
   *
   * Returns:
   *     True when added; false when the person cancelled
   */
  async function add(name: string): Promise<boolean> {
    const added = await fresh(async () => {
      const options = ok(await api.POST("/passkeys/registration/options", { body: { name } }));
      const credential = await create(options.public_key);
      if (credential === null) return false;
      ok(
        await api.POST("/passkeys", {
          body: { registration_id: options.registration_id, credential: credential as never },
        }),
      );
      return true;
    });
    await load();
    return added === true;
  }

  async function remove(id: string): Promise<boolean> {
    const removed = await fresh(async () => {
      done(await api.DELETE("/passkeys/{passkey_id}", { params: { path: { passkey_id: id } } }));
      return true;
    });
    await load();
    return removed === true;
  }

  return { passkeys, load, add, remove };
});
