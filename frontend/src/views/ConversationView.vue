<script setup lang="ts">
import { defineAsyncComponent, onMounted, ref } from "vue";
import { useSessionStore } from "../stores/session";
import MessageStream from "../components/MessageStream.vue";
import Composer from "../components/Composer.vue";
import PlanetDock from "../components/PlanetDock.vue";
import SettingsFloat from "../components/SettingsFloat.vue";

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
    <!-- 错误 / 警告分开表达：错误要查，警告只需知道 -->
    <div v-if="session.lastError" class="notice err" role="alert">
      <span class="kind mono">错误</span>
      <span class="text">{{ session.lastError }}</span>
      <router-link to="/debug" class="link">查看详情</router-link>
      <router-link to="/settings" class="link">前往设置</router-link>
    </div>
    <div v-else-if="session.warning" class="notice warn" role="status">
      <span class="kind mono">提示</span>
      <span class="text">{{ session.warning }}</span>
    </div>
    <MessageStream />
    <Composer />
    <SettingsFloat />
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
.notice {
  display: flex;
  align-items: center;
  gap: 12px;
  /* 右侧留白避开右上角浮动 ⚙（44px 按钮 + 26px 边距） */
  padding: 8px 84px 8px 20px;
  background: var(--bg-surface);
  font-size: 12px;
  flex-shrink: 0;
}
.notice.err {
  border-bottom: 1px solid var(--border-danger);
  color: var(--danger);
}
.notice.warn {
  border-bottom: 1px solid var(--warning);
  color: var(--warning);
}
.notice .kind {
  font-size: 10.5px;
  letter-spacing: 0.08em;
  padding: 1px 8px;
  border-radius: var(--r-pill);
  border: 1px solid currentColor;
  flex-shrink: 0;
}
.notice .text {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.notice .link {
  color: var(--link);
  text-decoration: none;
  flex-shrink: 0;
  letter-spacing: 0.04em;
}
.notice .link:hover {
  text-decoration: underline;
}
</style>
