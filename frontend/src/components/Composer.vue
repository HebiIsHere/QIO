<script setup lang="ts">
import { computed, ref } from "vue";
import { useSessionStore } from "../stores/session";

const session = useSessionStore();
const text = ref("");
const inputRef = ref<HTMLTextAreaElement | null>(null);
const memoryStrength = ref(0.38);

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
  <div class="composer">
    <div class="topicbar">
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
.composer {
  border-top: 1px solid var(--border-subtle);
  padding: 12px 44px 18px;
  background: var(--bg-surface);
  flex-shrink: 0;
}
.topicbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  font-size: 12px;
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