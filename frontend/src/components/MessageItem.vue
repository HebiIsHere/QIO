<script setup lang="ts">
import { computed, ref } from "vue";
import MarkdownContent from "./MarkdownContent.vue";
import { useSessionStore } from "../stores/session";
import { useEventStore } from "../stores/events";
import { useUiStore } from "../stores/ui";
import type { StreamMessage } from "../stores/session";

const props = defineProps<{ message: StreamMessage; showTopic?: boolean }>();
const session = useSessionStore();
const events = useEventStore();
const ui = useUiStore();

/**
 * token 下缀：只显示「这条消息所属 turn」的用量（turn_id 归属），
 * 且只在开发者模式显示。后端当前只提供单 turn 总 token，
 * 没有 input/output 分解就不显示分解数字（不编造）。
 */
const tokText = computed(() => {
  if (!ui.developerMode) return "";
  const usage = events.turnUsageFor(props.message.turnId);
  if (!usage) return "";
  return `token ${usage.tokens.toLocaleString("en-US")}`;
});

const open = ref(false);
const copied = ref(false);
let copyTimer: ReturnType<typeof setTimeout> | null = null;

/** 复制消息内容（剪贴板不可用时仍给出反馈，用户可手动复制） */
async function copyContent() {
  const text = props.message.content ?? "";
  if (!text) return;
  try {
    await navigator.clipboard?.writeText(text);
  } catch {
    // 忽略：无剪贴板权限时不做二次报错
  }
  copied.value = true;
  if (copyTimer) clearTimeout(copyTimer);
  copyTimer = setTimeout(() => {
    copied.value = false;
    copyTimer = null;
  }, 1600);
}

/** 呈现优先：title 兜底工具名 */
const toolTitle = computed(
  () => props.message.presentation?.title || props.message.toolName || "工具调用",
);

/** 呈现优先：status 作为语义状态徽标 */
const toolStatus = computed(() => props.message.presentation?.status || "");

/** 原始工具名：只用于悬停提示 / 排查，不作为界面标题（界面标题是中文展示名） */
const rawToolName = computed(
  () => props.message.presentation?.tool || props.message.toolName || "工具",
);

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
  // 只展示话题名，不暴露内部话题 ID（此前 slice(-4) 会显示"话题 #5320"这类编号，
  // 用户误以为是回答内容）
  return props.message.topicName || session.topicName || "默认话题";
});

/** 单行 metadata：时间 +（仅开发者模式）本 turn token */
const metaText = computed(() => {
  const time = formatTime(props.message.createdAt);
  return tokText.value ? `${time} · ${tokText.value}` : time;
});
</script>

<template>
  <div class="message" :class="message.role">
    <template v-if="message.role === 'user'">
      <div class="bubble user-bubble">
        <div class="plain">{{ message.content }}</div>
      </div>
      <div class="meta mono">
        <span v-if="message.queued" class="queued-tag">等待中</span>
        <span class="ts">{{ formatTime(message.createdAt) }}</span>
        <button v-if="!message.queued" class="copy-btn" type="button" @click="copyContent">
          {{ copied ? "已复制" : "复制" }}
        </button>
      </div>
    </template>

    <template v-else-if="message.role === 'tool'">
      <div
        class="tool-card"
        :class="{ fail: message.toolOk === false }"
        :title="rawToolName"
      >
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
          <span class="tool-time mono">{{ formatTime(message.createdAt) }}</span>
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
        <MarkdownContent :source="message.content" :reveal="!!message.streaming" :cps="ui.typewriterCps" />
      </div>
      <div class="meta mono">
        <span class="ts">{{ metaText }}</span>
        <button class="copy-btn" type="button" @click="copyContent">
          {{ copied ? "已复制" : "复制" }}
        </button>
      </div>
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
  /* 用户消息收窄：靠 alignment + 字色区分角色，不用整块色填充 */
  max-width: min(620px, 88%);
}
.message.assistant {
  margin-right: auto;
  align-items: flex-start;
}
.bubble {
  padding: 10px 16px;
}
/* 用户侧：无底色，右侧玫红细规线 + 右对齐位置 —— editorial 对话而非 IM 气泡 */
.user-bubble {
  background: transparent;
  color: var(--text-strong);
  border-right: 2px solid var(--accent);
  border-radius: 0;
  padding: 2px 14px 2px 16px;
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
/* 元数据：低调存在，hover / focus 时才完全显形（第一眼只看内容） */
.meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 6px;
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.05em;
  opacity: 0.62;
  transition: opacity var(--dur-fast) var(--ease);
}
.message:hover .meta,
.message:focus-within .meta {
  opacity: 1;
}
.ts {
  color: var(--text-muted);
}
.queued-tag {
  color: var(--text-secondary);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  padding: 0 8px;
  letter-spacing: 0.06em;
}
.copy-btn {
  border: none;
  background: none;
  padding: 0 2px;
  font: inherit;
  color: var(--text-muted);
  cursor: pointer;
  opacity: 0;
  transition: opacity var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.message:hover .copy-btn,
.copy-btn:focus-visible {
  opacity: 1;
}
.copy-btn:hover {
  color: var(--accent);
}
.copy-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
  border-radius: var(--r-xs);
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
