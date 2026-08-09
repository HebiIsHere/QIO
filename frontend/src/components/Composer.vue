<script setup lang="ts">
import { computed, ref } from "vue";
import { useSessionStore } from "../stores/session";
import { useFloatingWindow } from "../composables/useFloatingWindow";

const session = useSessionStore();
const text = ref("");
const inputRef = ref<HTMLTextAreaElement | null>(null);
const elRef = ref<HTMLElement | null>(null);
const headerRef = ref<HTMLElement | null>(null);
// 记忆强度：视觉占位（默认 0.38），待偏好设置接线
const memoryStrength = ref(0.38);

// 浮动窗口：贴边（默认底部居中），header（topicbar）为拖拽把手
useFloatingWindow(elRef, {
  id: "composer",
  dockMode: "edge",
  defaultPos: (el, vp) => ({
    x: Math.max(4, Math.round((vp.width - el.offsetWidth) / 2)),
    y: vp.height - el.offsetHeight - 18,
  }),
  dragHandle: headerRef,
});

const topicText = computed(() => session.topicName || (session.currentTopicId ? "当前话题" : "默认话题"));

const anchorText = computed(() => {
  const f = session.anchorFragment;
  if (f?.title) return `anchor · ${f.title}`;
  if (session.anchorFragmentId) return `anchor · 片段 #${session.anchorFragmentId.slice(-4)}`;
  return "";
});

function submit() {
  const value = text.value.trim();
  if (!value) return;
  session.send(value);
  text.value = "";
  if (inputRef.value) inputRef.value.style.height = "auto";
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submit();
  }
}

function autosize() {
  const el = inputRef.value;
  if (el) {
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 160) + "px";
  }
}
</script>

<template>
  <div ref="elRef" class="composer">
    <div ref="headerRef" class="topicbar fw-handle">
      <span class="tname serif" :title="session.currentTopicId ?? undefined">{{ topicText }}</span>
      <span v-if="anchorText" class="anchor mono">{{ anchorText }}</span>
      <span class="spacer"></span>
      <span class="slash mono">/tool /topic /memory</span>
    </div>

    <div class="input-row">
      <textarea
        ref="inputRef"
        v-model="text"
        class="qio-input"
        placeholder="和 QIO 说点什么…"
        :disabled="session.turnRunning"
        @keydown="onKeydown"
        @input="autosize"
      ></textarea>
      <button
        class="send-btn"
        type="button"
        :disabled="!text.trim() || session.turnRunning"
        :aria-label="session.turnRunning ? '运行中' : '发送'"
        :title="session.turnRunning ? '运行中' : '发送（Enter）'"
        @click="submit"
      >
        <span v-if="session.turnRunning">…</span>
        <span v-else>↑</span>
      </button>
    </div>

    <div class="mem-row">
      <span class="mem-label mono">记忆强度</span>
      <input
        v-model.number="memoryStrength"
        class="strength-slider"
        type="range"
        min="0"
        max="1"
        step="0.01"
        aria-label="记忆强度"
      />
      <span class="mem-val mono">{{ memoryStrength.toFixed(2) }}</span>
    </div>
  </div>
</template>

<style scoped>
/* 浮动窗口卡片（Task B）：定位由 useFloatingWindow 用 left/top 像素控制 */
.composer {
  position: fixed;
  z-index: 12;
  width: min(560px, calc(100vw - 32px));
  border: 1px solid var(--border-subtle);
  border-radius: 14px;
  padding: 10px 18px 16px;
  background: var(--bg-surface);
  box-shadow: 0 12px 34px rgba(0, 0, 0, 0.4);
  transition: transform 0.3s cubic-bezier(0.22, 0.8, 0.24, 1);
}
/* 贴靠隐藏：整体收起为底部细边（mouseenter 展开） */
.composer.fw-hidden {
  transform: translateY(calc(100% - 10px));
}
.topicbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  font-size: 12px;
}
/* 拖拽把手 */
.fw-handle {
  cursor: grab;
  user-select: none;
  -webkit-user-select: none;
  touch-action: none;
}
.fw-handle:active {
  cursor: grabbing;
}
.tname {
  font-weight: 600;
  color: var(--text-strong);
  font-size: 15px;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.anchor {
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.spacer {
  flex: 1;
}
.slash {
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.05em;
  white-space: nowrap;
}
.input-row {
  display: flex;
  align-items: flex-end;
  gap: 10px;
}
.input-row textarea.qio-input {
  flex: 1;
  min-height: 46px;
  max-height: 160px;
  resize: none;
}
.send-btn {
  width: 38px;
  height: 38px;
  flex-shrink: 0;
  border: none;
  border-radius: 50%;
  background: var(--accent);
  color: var(--on-accent);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  transition: background 0.18s, transform 0.15s;
}
.send-btn:hover:not(:disabled) {
  background: var(--accent-hover);
  transform: translateY(-1px);
}
.send-btn:disabled {
  opacity: 0.45;
  cursor: default;
}
.mem-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 10px;
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
.mem-label {
  white-space: nowrap;
}
.strength-slider {
  flex: 1;
  max-width: 220px;
  height: 4px;
  accent-color: var(--accent);
  cursor: pointer;
}
.mem-val {
  min-width: 34px;
  color: var(--text-secondary);
  text-align: right;
}
</style>
