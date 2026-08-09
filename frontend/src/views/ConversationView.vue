<script setup lang="ts">
import { defineAsyncComponent, onMounted, ref } from "vue";
import { useSessionStore } from "../stores/session";
import StatusBar from "../components/StatusBar.vue";
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
    <StatusBar />
    <MessageStream />
    <Composer />
    <PlanetDock @open="planetOpen = true" />
    <PlanetView v-if="planetOpen" @close="planetOpen = false" />
  </div>
</template>

<style scoped>
/* 分层：状态条/消息流/Composer 依次铺在 --bg-base 上，条与输入区用 --bg-surface */
.conversation {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-base);
  color: var(--text-primary);
  font-family: var(--sans);
}
</style>