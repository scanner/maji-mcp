<script setup lang="ts">
import type { Schemas } from "@/api/client";

defineProps<{ facts: Schemas["Fact"][] }>();
</script>

<template>
  <dl class="facts">
    <div v-for="(fact, index) in facts" :key="index" :class="{ flagged: fact.flag }">
      <dt>{{ fact.label }}</dt>
      <dd>
        {{ fact.value }}
        <strong v-if="fact.flag" class="flag">Worth a look</strong>
      </dd>
    </div>
  </dl>
</template>

<style scoped src="./FactList.css"></style>
