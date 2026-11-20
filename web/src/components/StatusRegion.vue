<script setup lang="ts">
import { useAnnouncer } from "@/stores/announcer";

const announcer = useAnnouncer();
</script>

<template>
  <div class="status" role="status" aria-live="polite">{{ announcer.status }}</div>
  <div class="alert" role="alert" aria-live="assertive">{{ announcer.alert }}</div>
</template>

<style scoped src="./StatusRegion.css"></style>
