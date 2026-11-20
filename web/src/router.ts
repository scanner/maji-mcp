/**
 * The client's pages, and the two things every navigation does: send a signed-out
 * browser to sign in, and move focus to the new page's heading, so a screen reader
 * announces where you are.
 */
import type { Pinia } from "pinia";
import { nextTick } from "vue";
import { createRouter, createWebHistory } from "vue-router";

import { useSession } from "@/stores/session";
import ConnectionsView from "@/views/ConnectionsView.vue";
import GrantsView from "@/views/GrantsView.vue";
import PasskeysView from "@/views/PasskeysView.vue";
import QueueView from "@/views/QueueView.vue";
import SignInView from "@/views/SignInView.vue";

export function createAppRouter(pinia: Pinia) {
  const router = createRouter({
    history: createWebHistory(),
    routes: [
      { path: "/", redirect: "/queue" },
      { path: "/queue", name: "queue", component: QueueView, meta: { title: "Queue" } },
      { path: "/grants", name: "grants", component: GrantsView, meta: { title: "Grants" } },
      {
        path: "/connections",
        name: "connections",
        component: ConnectionsView,
        meta: { title: "Connections" },
      },
      { path: "/passkeys", name: "passkeys", component: PasskeysView, meta: { title: "Passkeys" } },
      { path: "/sign-in", name: "sign-in", component: SignInView, meta: { title: "Sign in" } },
      { path: "/:rest(.*)*", redirect: "/queue" },
    ],
  });

  router.beforeEach(async (to) => {
    if (to.name === "sign-in") return true;
    const session = useSession(pinia);
    const state = session.state === "unknown" ? await session.check() : session.state;
    return state === "signed-in" ? true : { name: "sign-in", query: { next: to.fullPath } };
  });

  router.afterEach(async (to) => {
    document.title = `${String(to.meta.title ?? "Queue")} · maji-mcp`;
    await nextTick();
    document.querySelector<HTMLElement>("[data-page-heading]")?.focus();
  });

  return router;
}
