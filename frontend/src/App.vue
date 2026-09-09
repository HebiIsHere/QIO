<script setup lang="ts">
import { onMounted } from "vue";
import { useEventStore } from "./stores/events";
import { useUiStore } from "./stores/ui";
import ApprovalModal from "./components/ApprovalModal.vue";

const events = useEventStore();
const ui = useUiStore();
onMounted(() => {
  events.connect();
  void ui.load();
});
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
