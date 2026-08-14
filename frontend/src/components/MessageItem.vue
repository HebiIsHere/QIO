<script setup lang="ts">
import { computed, ref } from "vue";
import MarkdownContent from "./MarkdownContent.vue";
import { useSessionStore } from "../stores/session";
import { useEventStore } from "../stores/events";
import type { StreamMessage } from "../stores/session";

const props = defineProps<{ message: StreamMessage; showTopic?: boolean }>();
const session = useSessionStore();
const events = useEventStore();

/** token 用量下缀：USAGE 事件累计值（单条消息粒度未接，统一显示当前累计） */
const tokText = computed(() => `tok ${events.usageTokens.toLocaleString("en-US")}`);

const open = ref(false);

/** 呈现优先：title 兜底工具名 */
const toolTitle = computed(
  () => props.message.presentation?.title || props.message.toolName || "工具调用",
);

/** 呈现优先：status 作为语义状态徽标 */
const toolStatus = computed(() => props.message.presentation?.status || "");

/** 呈现优先：summary 兜底原始内容预览 */
const toolSummary = computed(
  () => props.message.presentation?.summary || props.message.content || "",
);

function formatTime(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const topicLine = computed(() => {
  const name = session.topicName || "默认话题";
  const id = session.currentTopicId;
  return id ? `${name} · 话题 #${id.slice(-4)}` : name;
});
</script>

<template>
  <div class="message" :class="message.role">
    <template v-if="message.role === 'user'">
      <div class="bubble user-bubble">
        <div class="plain">{{ message.content }}</div>
      </div>
      <div class="ts mono">{{ formatTime(message.createdAt) }} · {{ tokText }}</div>
    </template>

    <template v-else-if="message.role === 'tool'">
      <div class="tool-card" :class="{ fail: message.toolOk === false }">
        <button
          class="tool-head"
          type="button"
          @click="open = !open"
          :aria-expanded="open"
          :title="open ? '收起' : '展开'"
        >
          <span class="tool-mark" :class="message.toolOk === false ? 'fail' : 'ok'">
            {{ message.toolOk === false ? "✕" : "✓" }}
          </span>
          <span class="tool-name mono">{{ toolTitle }}</span>
          <span v-if="toolStatus" class="tool-status" :class="message.toolOk === false ? 'fail' : 'ok'">
            {{ toolStatus }}
          </span>
          <span class="tool-time mono">{{ formatTime(message.createdAt) }} · {{ tokText }}</span>
          <span class="tool-chev">{{ open ? "▾" : "▸" }}</span>
        </button>
        <div v-show="open" class="tool-detail">
          <pre>{{ toolSummary }}</pre>
          <p v-if="message.toolError" class="tool-error">{{ message.toolError }}</p>
        </div>
      </div>
    </template>

    <template v-else>
      <div class="bubble assist-bubble" :class="{ interim: message.interim }">
        <div v-if="message.interim" class="interim-tag mono">◈ 过程</div>
        <div v-if="showTopic" class="tname serif">{{ topicLine }}</div>
        <div v-if="message.memoryInject" class="inject-tag">◈ {{ message.memoryInject.label }}</div>
        <MarkdownContent :source="message.content" />
      </div>
      <div class="ts mono">{{ formatTime(message.createdAt) }} · {{ tokText }}</div>
    </template>
  </div>
</template>

<style scoped>
.message {
  display: flex;
  flex-direction: column;
  /* 左半边完整显示：比原 640px 更宽，宽屏下消息占左侧更充分 */
  max-width: min(760px, 100%);
  margin: 6px 0;
  font-size: 14.5px;
  line-height: 1.75;
}
.message.user {
  margin-left: auto;
  align-items: flex-end;
}
.message.assistant {
  margin-right: auto;
  align-items: flex-start;
}
.bubble {
  padding: 10px 16px;
}
.user-bubble {
  background: var(--accent);
  color: var(--on-accent);
  border-radius: 14px 14px 4px 14px;
  text-align: left;
}
.user-bubble .plain {
  white-space: pre-wrap;
}
.assist-bubble {
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: 14px 14px 14px 4px;
}
.tname {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-strong);
  margin-bottom: 6px;
}
.assist-bubble.interim {
  background: transparent;
  border-style: dashed;
}
.interim-tag {
  display: inline-flex;
  align-items: center;
  margin-bottom: 6px;
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.05em;
}
.inject-tag {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin: 2px 0 8px;
  padding: 3px 10px;
  border: 1px dashed var(--link);
  color: var(--link);
  border-radius: 20px;
  font-family: var(--mono);
  font-size: 10.5px;
  letter-spacing: 0.04em;
}
.ts {
  font-size: 10.5px;
  color: var(--text-muted);
  margin-top: 6px;
  letter-spacing: 0.05em;
}
/* ---- 工具卡 ---- */
.tool-card {
  margin: 4px 0;
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  background: var(--bg-elevated);
  overflow: hidden;
  font-size: 12.5px;
}
.tool-card.fail {
  border-color: var(--border-danger);
}
.tool-head {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 12px;
  background: transparent;
  border: none;
  cursor: pointer;
  text-align: left;
  font-size: 11px;
  color: var(--text-secondary);
}
.tool-head:hover {
  background: var(--accent-soft);
}
.tool-mark {
  font-size: 12px;
}
.tool-mark.ok {
  color: var(--success);
}
.tool-mark.fail {
  color: var(--danger);
}
.tool-name {
  color: var(--text-strong);
  letter-spacing: 0.02em;
}
.tool-status {
  padding: 1px 8px;
  border-radius: 20px;
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.04em;
  border: 1px solid currentColor;
}
.tool-status.ok {
  color: var(--success);
}
.tool-status.fail {
  color: var(--danger);
}
.tool-time {
  margin-left: auto;
  color: var(--text-muted);
  letter-spacing: 0.05em;
}
.tool-chev {
  color: var(--text-muted);
  flex-shrink: 0;
}
.tool-detail {
  border-top: 1px solid var(--border-subtle);
  padding: 10px 12px;
  background: var(--bg-inset);
}
.tool-detail pre {
  font-size: 11px;
  white-space: pre-wrap;
  color: var(--text-secondary);
  margin: 0;
  font-family: var(--mono);
}
.tool-error {
  color: var(--danger);
  font-size: 12px;
  margin: 4px 0 0;
}
</style>
