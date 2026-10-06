<!-- 右下悬浮聊天（子智能体 C 负责实现；这里是骨架占位）。 -->
<script setup lang="ts">
import { useInteractiveStore } from "../../stores/interactive";

const store = useInteractiveStore();
</script>

<template>
  <div class="chat-dock-placeholder">
    <button class="dock-btn" type="button" data-im="chat-toggle" @click="store.chatOpen = !store.chatOpen">
      {{ store.chatOpen ? "收起对话" : "对话" }}
    </button>
    <section v-if="store.chatOpen" class="dock-panel" data-im="chat-panel">
      <p class="hint">悬浮对话：复用当前 QIO 会话，只发送输入的文字。</p>
      <p class="hint mono">（由子智能体 C 接管；此处为骨架占位。）</p>
    </section>
  </div>
</template>

<style scoped>
.chat-dock-placeholder {
  position: absolute;
  right: var(--sp-4);
  bottom: var(--sp-4);
  z-index: 25;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--sp-2);
}
.dock-btn {
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--on-accent);
  background: var(--accent);
  border: none;
  border-radius: var(--r-pill);
  padding: var(--sp-2) var(--sp-4);
  cursor: pointer;
}
.dock-panel {
  width: min(360px, 88vw);
  padding: var(--sp-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
  color: var(--text-secondary);
}
.hint { margin: 0 0 var(--sp-1); font-size: var(--fs-sm); }
</style>
