<script setup lang="ts">
import { computed, ref } from "vue";
import { useSessionStore } from "../stores/session";

const session = useSessionStore();
const text = ref("");
const inputRef = ref<HTMLTextAreaElement | null>(null);

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
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 160) + "px";
}
</script>

<template>
  <div class="composer bubble">
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
  </div>
</template>

<style scoped>
/* 贴右下角大气泡：浅色表面 + 玫红描边 + 右下小圆角，固定不可拖动 */
.composer {
  position: fixed;
  right: 16px;
  bottom: 16px;
  z-index: 12;
  width: min(560px, calc(100vw - 32px));
  border: 1.5px solid var(--accent);
  border-radius: 18px 18px 4px 18px;
  padding: 10px 18px 16px;
  background: var(--bg-surface);
  box-shadow: 0 12px 34px rgba(0, 0, 0, 0.4);
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
</style>
