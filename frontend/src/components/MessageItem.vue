<script setup lang="ts">
import MarkdownContent from "./MarkdownContent.vue";
import type { StreamMessage } from "../stores/session";

defineProps<{ message: StreamMessage }>();
const showToolDetail = (id: string) => {
  const el = document.getElementById(`tool-${id}`);
  if (el) el.hidden = !el.hidden;
};
</script>

<template>
  <div class="message" :class="message.role">
    <div class="bubble">
      <template v-if="message.role === 'user'">
        <div class="plain">{{ message.content }}</div>
      </template>
      <template v-else-if="message.role === 'tool'">
        <div class="tool-card" :class="{ fail: !message.toolOk }">
          <div class="tool-head" @click="showToolDetail(message.id)">
            <span class="tool-icon">⚙</span>
            <span class="tool-name">{{ message.toolName || "工具调用" }}</span>
            <span class="tool-status" :class="{ ok: message.toolOk !== false, fail: message.toolOk === false }">
              {{ message.toolOk === false ? "失败" : "成功" }}
            </span>
          </div>
          <div :id="`tool-${message.id}`" class="tool-detail" hidden>
            <pre>{{ message.content }}</pre>
            <p v-if="message.toolError" class="tool-error">{{ message.toolError }}</p>
          </div>
        </div>
      </template>
      <template v-else>
        <MarkdownContent :source="message.content" />
      </template>
    </div>
  </div>
</template>

<style scoped>
.message { display: flex; margin: 10px 0; }
.message.user { justify-content: flex-end; }
.bubble {
  max-width: 78%; padding: 10px 14px; border-radius: 12px;
  background: var(--bg-elevated); border: 1px solid var(--border-subtle);
}
.message.user .bubble { background: var(--bg-accent-subtle); border-color: var(--border-strong); }
.plain { white-space: pre-wrap; font-size: 14px; }
.tool-card { border: 1px solid var(--border-subtle); border-radius: 8px; overflow: hidden; }
.tool-card.fail { border-color: var(--border-danger); }
.tool-head { display: flex; align-items: center; gap: 8px; padding: 6px 10px; cursor: pointer; font-size: 12px; }
.tool-icon { color: var(--text-secondary); }
.tool-name { font-weight: 600; color: var(--text-primary); }
.tool-status.ok { color: var(--success); }
.tool-status.fail { color: var(--danger); }
.tool-detail { border-top: 1px solid var(--border-subtle); padding: 8px 10px; }
.tool-detail pre { font-size: 12px; white-space: pre-wrap; color: var(--text-secondary); margin: 0; }
.tool-error { color: var(--danger); font-size: 12px; margin: 4px 0 0; }
</style>