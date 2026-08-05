<script setup lang="ts">
import { onMounted } from "vue";
import { useEventStore } from "./stores/events";
import ApprovalModal from "./components/ApprovalModal.vue";

const events = useEventStore();
onMounted(() => events.connect());
</script>

<template>
  <div class="app-shell">
    <router-view />
    <ApprovalModal />
  </div>
</template>

<style>
:root {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  color: var(--text-primary);
  background: var(--bg-base);
}
* { box-sizing: border-box; }
body { margin: 0; }
.app-shell { width: 100vw; height: 100vh; overflow: hidden; }
</style>