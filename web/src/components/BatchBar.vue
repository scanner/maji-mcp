<script setup lang="ts">
defineProps<{ count: number; busy: boolean }>();
const emit = defineEmits<{ approve: []; clear: [] }>();
</script>

<template>
  <section class="batch" aria-label="Batch approval">
    <p>
      <strong>{{ count }} selected.</strong>
      One approval covers them all, each as you saw it.
    </p>
    <div class="buttons">
      <button type="button" class="primary" :disabled="busy" @click="emit('approve')">
        Approve {{ count }} {{ count === 1 ? "item" : "items" }}
      </button>
      <button type="button" class="neutral" :disabled="busy" @click="emit('clear')">
        Clear the selection
      </button>
    </div>
  </section>
</template>

<style scoped src="./BatchBar.css"></style>
