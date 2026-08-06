<script setup lang="ts">
import { ref } from "vue";
import { useSessionStore } from "../stores/session";

const session = useSessionStore();
const text = ref("");
const inputRef = ref<HTMLTextAreaElement | null>(null);

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
    <div class="meta-row">
      <span class="topic" :title="session.currentTopicId ?? undefined">
        话题：{{ session.currentTopicId ? "当前话题" : "默认话题" }}
      </span>

    </div>
    <textarea
      ref="inputRef"
      v-model="text"
      placeholder="输入消息，Enter 发送，Shift+Enter 换行"
      :disabled="session.turnRunning"
      @keydown="onKeydown"
      @input="autosize"
    ></textarea>
    <div class="actions">
      <button :disabled="!text.trim() || session.turnRunning" @click="submit">
        {{ session.turnRunning ? "运行中…" : "发送" }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.composer { border-top: 1px solid var(--border-subtle); padding: 10px 14px; background: var(--bg-surface); }
.meta-row { display: flex; align-items: center; gap: 16px; font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; }
.strength { display: flex; align-items: center; gap: 6px; }
.strength input { width: 120px; accent-color: var(--accent); }
.strength .value { min-width: 32px; color: var(--text-primary); }
textarea {
  width: 100%; min-height: 44px; max-height: 160px; resize: none;
  background: var(--bg-inset); color: var(--text-primary); border: 1px solid var(--border-subtle);
  border-radius: 10px; padding: 10px 12px; font-size: 14px; font-family: inherit;
}
textarea:focus { outline: none; border-color: var(--accent); }
.actions { display: flex; justify-content: flex-end; margin-top: 8px; }
.actions button {
  background: var(--accent); color: var(--on-accent); border: none; border-radius: 18px;
  padding: 7px 22px; cursor: pointer; font-size: 13px;
}
.actions button:hover:not(:disabled) { background: var(--accent-hover); }
.actions button:disabled { opacity: 0.45; cursor: default; }
</style>