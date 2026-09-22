<script setup lang="ts">
/**
 * 执行叙事（Execution Narrative）的一行：**抽屉头 + 收纳的调用记录**。
 *
 * 设计契约（spec 2026-09-22 §8）：
 *
 * * 默认收起，运行中也**不自动展开** —— 只有用户点击才打开；
 * * 折叠头必须如实说出里面有什么：`N 次调用` / `N 运行中` / `N 失败` / `N 已取消`
 *   （收纳不等于藏起异常）；
 * * 实时轮次的抽屉里是系统工具卡；历史轮次的抽屉里是系统生成的调用摘要
 *   （`raw.calls`），两者都不含模型自由文本；
 * * 模型文案只出现在这一行的 `ntext` 里，工具事实、风险、审批权限都不经过这里。
 */
import { computed, ref } from "vue";
import type { StreamMessage } from "../stores/session";

const props = defineProps<{
  narrative: StreamMessage;
  /** 实时轮次里被这一行收纳的调用卡 */
  calls?: StreamMessage[];
}>();

const open = ref(false);

interface Row {
  id: string;
  tool: string;
  title: string;
  status: "running" | "success" | "failed" | "cancelled";
  durationMs: number | null;
  error: string | null;
}

function statusOf(message: StreamMessage): Row["status"] {
  if (message.toolStatus) return message.toolStatus === "unknown" ? "failed" : message.toolStatus;
  if (message.toolRunning) return "running";
  return message.toolOk === false ? "failed" : "success";
}

/** 实时轮次优先用工具卡的真实状态；历史轮次用系统写的调用摘要。 */
const rows = computed<Row[]>(() => {
  const live = props.calls ?? [];
  if (live.length) {
    return live.map((m) => ({
      id: m.callId || m.id,
      tool: m.presentation?.tool || m.toolName || "",
      title: m.presentation?.title || m.toolName || "工具调用",
      status: statusOf(m),
      durationMs: typeof m.toolDurationMs === "number" ? m.toolDurationMs : null,
      error: m.toolError ?? null,
    }));
  }
  return (props.narrative.narrativeCalls ?? []).map((c) => ({
    id: c.callId || c.tool,
    tool: c.tool,
    title: c.title || c.tool,
    status: c.status,
    durationMs: typeof c.durationMs === "number" ? c.durationMs : null,
    error: c.error ?? null,
  }));
});

const historyRows = computed(() => ((props.calls ?? []).length ? [] : rows.value));
const running = computed(() => rows.value.filter((r) => r.status === "running").length);
const failed = computed(() => rows.value.filter((r) => r.status === "failed").length);
const cancelled = computed(() => rows.value.filter((r) => r.status === "cancelled").length);
const durationMs = computed(() => {
  if (!rows.value.length || rows.value.some((r) => r.durationMs === null)) return null;
  return rows.value.reduce((sum, r) => sum + (r.durationMs ?? 0), 0);
});

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.max(1, Math.round(ms))}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/** 折叠头的状态文案：异常优先（失败 > 取消 > 运行中 > 完成）。 */
const metaText = computed(() => {
  if (!rows.value.length) return "";
  if (failed.value) return `${failed.value} 失败`;
  if (cancelled.value) return `${cancelled.value} 已取消`;
  if (running.value) return `${running.value} 运行中`;
  const total = durationMs.value;
  return total === null
    ? `${rows.value.length} 次调用`
    : `${rows.value.length} 次调用 · ${formatDuration(total)}`;
});

const hasDrawer = computed(() => rows.value.length > 0);
const mark = computed(() =>
  (props.narrative.narrativeKind ?? "progress") === "warning" ? "!" : "◈",
);
</script>

<template>
  <div
    class="qio-narrative"
    :data-kind="narrative.narrativeKind ?? 'progress'"
    :data-open="open ? 'true' : 'false'"
  >
    <button
      v-if="hasDrawer"
      class="nhead"
      type="button"
      :aria-expanded="open ? 'true' : 'false'"
      :title="open ? '收起这一步的调用记录' : '展开这一步的调用记录'"
      @click="open = !open"
    >
      <span class="nmark" aria-hidden="true">{{ mark }}</span>
      <span class="ntext">{{ narrative.content }}</span>
      <span
        v-if="metaText"
        class="nmeta mono"
        :class="{ run: running > 0, bad: failed > 0 || cancelled > 0 }"
      >{{ metaText }}</span>
      <span class="nchev" aria-hidden="true"></span>
    </button>
    <div v-else class="nhead static">
      <span class="nmark" aria-hidden="true">{{ mark }}</span>
      <span class="ntext">{{ narrative.content }}</span>
    </div>

    <div v-if="hasDrawer" class="ndrawer">
      <div class="ndrawer-clip">
        <div class="ndrawer-body">
          <slot />
          <template v-if="historyRows.length">
            <div class="calls-note mono">调用摘要 · 系统生成</div>
            <div v-for="row in historyRows" :key="row.id" class="callrow">
              <span class="tool-mark" :class="row.status === 'success' ? 'ok' : 'fail'" aria-hidden="true">
                {{ row.status === "success" ? "✓" : "✕" }}
              </span>
              <span class="callname mono">{{ row.tool || row.title }}</span>
              <span class="callmeta mono" :class="{ bad: row.status !== 'success' }">
                {{ row.error || (row.durationMs !== null ? formatDuration(row.durationMs) : row.status) }}
              </span>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 叙事行 = 抽屉头：左侧一条极淡的轨，把这一阶段的调用收在下面 */
.qio-narrative {
  margin: 3px 0 3px 6px;
  border-left: 1px solid var(--border-subtle);
}
.qio-narrative[data-kind="warning"] {
  border-left-color: var(--warning-soft);
}
.nhead {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  width: 100%;
  padding: 6px 2px 6px 13px;
  background: none;
  border: none;
  font: inherit;
  text-align: left;
  color: inherit;
  cursor: pointer;
  position: relative;
}
.nhead.static {
  cursor: default;
}
.nhead::before {
  content: "";
  position: absolute;
  left: 0;
  top: 15px;
  width: 9px;
  height: 1px;
  background: var(--border-subtle);
}
.nhead:hover .ntext {
  color: var(--text-strong);
}
.nhead:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: -2px;
  border-radius: var(--r-xs);
}
.nmark {
  flex: 0 0 auto;
  width: 16px;
  height: 16px;
  margin-top: 2px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 9px;
  line-height: 1;
  color: var(--link);
  border: 1px solid currentColor;
  background: var(--bg-base);
}
.ntext {
  font-size: 13.5px;
  line-height: 1.62;
  color: var(--text-secondary);
}
.qio-narrative[data-kind="announce"] .ntext,
.qio-narrative[data-kind="result"] .ntext {
  color: var(--text-primary);
}
.qio-narrative[data-kind="warning"] .nmark,
.qio-narrative[data-kind="warning"] .ntext {
  color: var(--warning);
}
/* 折叠头的状态读数永远不得大过内容字号 */
.nmeta {
  margin-left: auto;
  padding: 2px 0 0 10px;
  font-size: 10px;
  color: var(--text-muted);
  white-space: nowrap;
}
.nmeta.run {
  color: var(--link);
}
.nmeta.run::before {
  content: "";
  display: inline-block;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--link);
  margin-right: 5px;
  vertical-align: 1px;
  animation: nar-pulse 1.2s infinite;
}
.nmeta.bad {
  color: var(--danger);
}
@keyframes nar-pulse {
  0%, 100% { opacity: 0.25; }
  40% { opacity: 1; }
}
.nchev {
  flex: 0 0 auto;
  width: 0;
  height: 0;
  font-size: 0;
  margin: 8px 2px 0 8px;
  border-left: 5px solid currentColor;
  border-top: 4px solid transparent;
  border-bottom: 4px solid transparent;
  color: var(--text-secondary);
  transition: transform var(--mo-1-menu) var(--ease-1);
}
.nhead:hover .nchev {
  color: var(--text-strong);
}
.qio-narrative[data-open="true"] .nchev {
  transform: rotate(90deg);
}
.ndrawer {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows var(--mo-2-in) var(--ease-2);
}
.qio-narrative[data-open="true"] .ndrawer {
  grid-template-rows: 1fr;
}
.ndrawer-clip {
  overflow: hidden;
  min-height: 0;
  opacity: 0;
  transition: opacity var(--mo-2-in) var(--ease-2);
}
.qio-narrative[data-open="true"] .ndrawer-clip {
  opacity: 1;
}
.ndrawer-body {
  padding: 1px 0 8px 13px;
}
.calls-note {
  font-size: 10px;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  margin: 2px 0 6px;
}
.callrow {
  display: flex;
  align-items: center;
  gap: 9px;
  margin: 4px 0;
  padding: 5px 10px;
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  font-size: 12px;
  color: var(--text-secondary);
}
.tool-mark {
  font-size: 11px;
}
.tool-mark.ok {
  color: var(--success);
}
.tool-mark.fail {
  color: var(--danger);
}
.callname {
  font-size: 11.5px;
  color: var(--text-strong);
}
.callmeta {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--text-muted);
  white-space: nowrap;
}
.callmeta.bad {
  color: var(--danger);
}
@media (prefers-reduced-motion: reduce) {
  .ndrawer,
  .ndrawer-clip {
    transition-duration: 90ms;
  }
  .nmeta.run::before {
    animation: none;
  }
}
</style>
