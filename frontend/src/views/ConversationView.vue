<script setup lang="ts">
import { defineAsyncComponent, onMounted, ref } from "vue";
import { useSessionStore } from "../stores/session";
import MessageStream from "../components/MessageStream.vue";
import Composer from "../components/Composer.vue";
import PlanetDock from "../components/PlanetDock.vue";

// 星球页懒加载：three.js 不进首屏 chunk
const PlanetView = defineAsyncComponent(() => import("./PlanetView.vue"));
const planetOpen = ref(false);
const session = useSessionStore();

onMounted(() => {
  session.loadHistory();
});
</script>

<template>
  <div class="conversation">
    <!-- 异常提示条：仅在出错时出现（lastError），可跳设置页排查 -->
    <div v-if="session.lastError" class="err-hint" role="alert">
      <span class="err-text mono">{{ session.lastError }}</span>
      <router-link to="/settings" class="err-link">前往设置</router-link>
    </div>
    <MessageStream />
    <Composer />
    <PlanetDock @open="planetOpen = true" />
    <PlanetView v-if="planetOpen" @close="planetOpen = false" />
  </div>
</template>

<style scoped>
/* 分层：消息流/Composer 依次铺在 --bg-base 上，输入区用 --bg-surface；顶部无状态条 */
.conversation {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-base);
  color: var(--text-primary);
  font-family: var(--sans);
}
.err-hint {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 20px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-danger);
  color: var(--danger);
  font-size: 12px;
  flex-shrink: 0;
}
.err-hint .err-text {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.err-hint .err-link {
  color: var(--link);
  text-decoration: none;
  flex-shrink: 0;
  letter-spacing: 0.04em;
}
.err-hint .err-link:hover {
  text-decoration: underline;
}
</style>
