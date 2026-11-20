<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import { ApiError, api, ok } from "@/api/client";
import type { Connection } from "@/api/client";
import { when } from "@/lib/describe";
import { useAnnouncer } from "@/stores/announcer";
import { useQueue } from "@/stores/queue";

const announcer = useAnnouncer();
const queue = useQueue();
const router = useRouter();
const connections = ref<Connection[]>([]);
const loaded = ref(false);
const busy = ref(false);

const STATUS: Record<Connection["status"], string> = {
  connected: "Connected",
  not_connected: "Not connected",
  expired: "Its key has expired",
  revoked: "Its key was revoked",
};

onMounted(async () => {
  try {
    connections.value = ok(await api.GET("/connections")).connections;
  } catch (error) {
    announcer.warn(`The connections couldn't be loaded: ${(error as Error).message}.`);
  } finally {
    loaded.value = true;
  }
});

/** Open an item in the queue, where its key is pasted, or Disconnect is offered. */
async function openItem(id: string): Promise<void> {
  await queue.load();
  queue.open(id);
  await router.push({ name: "queue" });
}

/**
 * Ask to connect a service, or to replace its key: the Connect item is put in the
 * queue, or the one already waiting is opened.
 */
async function connect(connection: Connection): Promise<void> {
  busy.value = true;
  try {
    const item = ok(
      await api.POST("/connections/{service}/requests", {
        params: { path: { service: connection.service } },
      }),
    );
    await openItem(item.id);
  } catch (error) {
    const why = error instanceof ApiError ? error.message : "the control plane couldn't be reached";
    announcer.warn(`${connection.label} can't be connected now: ${why}.`);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="connections">
    <h1 data-page-heading tabindex="-1">Connections</h1>
    <p class="intro">
      The services scripts can call with a key you give maji-mcp. You paste the key into a
      Connect item in the queue and sign with your passkey; only the broker keeps it, and
      sends it only to the service's own host. The queue records the key's fingerprint,
      never the key.
    </p>

    <p v-if="loaded && !connections.length" class="empty">
      No services yet: the broker lists them when it starts.
    </p>
    <ul class="list">
      <li v-for="c in connections" :key="c.service">
        <div>
          <p class="name">{{ c.label }}</p>
          <p class="meta">
            {{ STATUS[c.status] }} · key sent only to {{ c.host }}
            <template v-if="c.connected_at">
              · connected <time :datetime="c.connected_at">{{ when(c.connected_at) }}</time>
            </template>
            <template v-if="c.fingerprint"> · key {{ c.fingerprint.slice(0, 15) }}</template>
            <template v-for="(value, name) in c.settings ?? {}" :key="name"> · {{ name }} {{ value }}</template>
          </p>
        </div>
        <div class="buttons">
          <button
            v-if="c.status !== 'connected' || !c.item"
            type="button"
            class="primary"
            :disabled="busy"
            :aria-label="`Connect ${c.label}`"
            @click="connect(c)"
          >
            Connect
          </button>
          <template v-else>
            <button
              type="button"
              class="neutral"
              :disabled="busy"
              :aria-label="`Replace the ${c.label} key`"
              @click="connect(c)"
            >
              Replace the key
            </button>
            <button
              type="button"
              class="neutral"
              :disabled="busy"
              :aria-label="`Open the item that connected ${c.label}, to disconnect it`"
              @click="openItem(c.item)"
            >
              Disconnect…
            </button>
          </template>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped src="./ConnectionsView.css"></style>
