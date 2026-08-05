<script setup lang="ts">
import { useEventStore } from "../stores/events";
import { useSessionStore } from "../stores/session";

const events = useEventStore();
const session = useSessionStore();
</script>

<template>
  <div class="status-bar">
    <span class="dot" :class="{ on: events.connected }"></span>
    <span class="text">{{ events.connected ? "已连接" : "连接中…" }}</span>
    <span v-if="session.turnRunning" class="running">agent 运行中…</span>
    <span v-if="session.lastError" class="error" :title="session.lastError">出错</span>
    <router-link to="/settings" class="settings-link">设置</router-link>
  </div>
</template>

<style scoped>
.status-bar {
  display: flex; align-items: center; gap: 8px;
  height: 32px; padding: 0 12px;
  background: var(--bg-surface); border-bottom: 1px solid var(--border-subtle);
  font-size: 12px; color: var(--text-secondary);
}
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-muted); }
.dot.on { background: var(--success); }
.running { color: var(--warning); }
.error { color: var(--danger); }
.settings-link { margin-left: auto; color: var(--link); text-decoration: none; cursor: pointer; }
</style>