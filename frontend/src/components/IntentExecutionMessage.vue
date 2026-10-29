<script setup lang="ts">
/**
 * 意图执行记录（R2 契约 §四）。
 *
 * 只渲染服务端 `intent_execution` 对象里的**事实**：
 * - 默认一行：意图名称 + 服务端 status_label + 查看详情；
 * - 详情：处理范围 / 执行经过（attempts 时间线）/ 逐项状态（批量）/ 结果入口 / 失败原因 / 诊断编号；
 * - record_head !== true 的重复尝试不渲染（同一次确认不刷屏；每次 attempt 在头部详情里可见）；
 * - verified === false 时显示服务端 verified_note，不把无法核对说成已完成；
 * - 不按文本前缀/关键词判断、不自行推状态、不新增弹窗。
 */
import { computed, ref, useId } from "vue";
import type {
  IntentExecutionAttempt,
  IntentExecutionIntent,
  IntentExecutionRecord,
  IntentExecutionResultRef,
} from "../services/api";
import { isSafeExternalUrl, openExternal } from "../utils/externalLink";

const props = defineProps<{ record: IntentExecutionRecord }>();

const open = ref(false);
const detailId = "intent-execution-detail-" + useId();
const resultNotice = ref("");

/** 服务端给的状态枚举 → 中文兜底（只在 status_label 缺失时用于翻译，不推断状态） */
const STATUS_TEXT: Record<string, string> = {
  confirmed: "已确认，等待处理",
  queued: "等待处理",
  running: "正在处理",
  partial: "部分完成",
  failed: "处理失败",
  completed: "已完成",
  unknown: "状态无法核对",
};

/** 尝试来源 → 中文（来源也是服务端事实，label 缺失时按枚举翻译） */
const SOURCE_TEXT: Record<string, string> = {
  confirm: "确认执行",
  resume: "继续执行",
  retry: "重试",
};

function textOf(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function statusTextOf(status: string | undefined, label: string | undefined): string {
  const explicit = textOf(label);
  if (explicit) return explicit;
  const key = textOf(status);
  if (!key) return "状态未提供";
  return STATUS_TEXT[key] ?? key;
}

function sourceTextOf(source: string | undefined): string {
  const key = textOf(source);
  if (!key) return "";
  return SOURCE_TEXT[key] ?? key;
}

/** 状态 tone 只决定视觉颜色，不参与状态判断 */
function toneOf(status: string | undefined): "ok" | "warn" | "err" | "info" | "quiet" {
  switch (textOf(status)) {
    case "completed":
      return "ok";
    case "partial":
      return "warn";
    case "failed":
      return "err";
    case "confirmed":
    case "queued":
    case "running":
      return "info";
    default:
      return "quiet";
  }
}

const title = computed(() => {
  const explicit = textOf(props.record.title);
  if (explicit) return explicit;
  const first = (props.record.intents ?? []).find((item) => textOf(item.title));
  return textOf(first?.title) || "意图执行记录";
});

const statusLabel = computed(() => statusTextOf(props.record.status, props.record.status_label));
const statusTone = computed(() => toneOf(props.record.status));

const scopeBoard = computed(() => textOf(props.record.scope?.board_name));
const scopeIntents = computed(() =>
  (props.record.scope?.intent_titles ?? []).map(textOf).filter(Boolean),
);
const scopeSource = computed(() => sourceTextOf(props.record.scope?.source));

/** 执行经过：每一次真实 attempt（自动重试也在这里，不新起一张卡） */
const attemptRows = computed(() =>
  (props.record.attempts ?? []).map((attempt: IntentExecutionAttempt, index: number) => ({
    key: (attempt.turn_id || "attempt") + "-" + index,
    index: typeof attempt.attempt === "number" ? attempt.attempt : index + 1,
    source: sourceTextOf(attempt.source),
    status: statusTextOf(attempt.status, undefined),
    at: formatTime(attempt.started_at),
    reason: textOf(attempt.reason),
  })),
);

/** 逐项状态只对批量记录展示；单项时名称/状态已在默认行 */
const intentRows = computed(() =>
  (props.record.intents ?? []).map((intent: IntentExecutionIntent, index: number) => ({
    key: intent.id || "intent-" + index,
    title: textOf(intent.title) || "未命名意图",
    status: statusTextOf(intent.status, intent.status_label),
    tone: toneOf(intent.status),
    attempts: typeof intent.attempts === "number" ? intent.attempts : null,
    failure: textOf(intent.failure_reason),
    resultLabel: textOf(intent.result_ref?.label),
  })),
);

/** 失败原因：逐项真实失败原因去重；attempts 的失败原因在时间线里各自可见 */
const failureReasons = computed(() => {
  const out: string[] = [];
  for (const intent of props.record.intents ?? []) {
    const reason = textOf(intent.failure_reason);
    if (reason && !out.includes(reason)) out.push(reason);
  }
  return out;
});

const resultEntry = computed<IntentExecutionResultRef | null>(() => props.record.result_entry ?? null);
const resultLabel = computed(() => textOf(resultEntry.value?.label) || "查看结果");
const resultTarget = computed(() => textOf(resultEntry.value?.target));
const resultHref = computed(() =>
  isSafeExternalUrl(resultTarget.value) ? resultTarget.value : "",
);

async function openResult() {
  if (!resultTarget.value) return;
  if (!resultHref.value) {
    resultNotice.value = "这个结果入口的地址不能直接打开";
    return;
  }
  const ok = await openExternal(resultTarget.value);
  resultNotice.value = ok ? "" : "没能打开结果入口（当前环境不支持或链接被拦截）";
}

/** 诊断编号：内部标识只允许出现在这里（等宽小字），默认行不出现 */
const diagnosticsRows = computed(() => {
  const diag = props.record.diagnostics ?? {};
  const rows: { label: string; value: string }[] = [];
  const push = (label: string, value: string) => {
    if (value) rows.push({ label, value });
  };
  push("板面编号", textOf(diag.board_id));
  push("提交编号", textOf(diag.submission_id));
  push("意图编号", (diag.intent_ids ?? []).map(textOf).filter(Boolean).join("、"));
  push("轮次编号", (diag.turn_ids ?? []).map(textOf).filter(Boolean).join("、"));
  push("记录标识", textOf(diag.record_key) || textOf(props.record.record_key));
  return rows;
});

/** verified=false：服务端必须给一句事实；没有就用一句不夸大的兜底，绝不显示成已完成 */
const verifiedWarning = computed(() => {
  if (props.record.verified !== false) return "";
  return textOf(props.record.verified_note) || "这组执行记录无法核对";
});

const verifiedNote = computed(() =>
  props.record.verified === false ? "" : textOf(props.record.verified_note),
);

function formatTime(iso?: string): string {
  const raw = textOf(iso);
  if (!raw) return "";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return (
    p(date.getMonth() + 1) + "-" + p(date.getDate()) + " " + p(date.getHours()) + ":" + p(date.getMinutes())
  );
}
</script>

<template>
  <section
    v-if="record.record_head === true"
    class="intent-execution qio-card"
    data-test="intent-execution"
    :data-status="record.status || 'unknown'"
  >
    <button
      class="intent-execution-head"
      type="button"
      data-test="intent-execution-head"
      :aria-expanded="open ? 'true' : 'false'"
      :aria-controls="detailId"
      @click="open = !open"
    >
      <span class="intent-execution-tag qio-tag">意图执行</span>
      <span class="intent-execution-title">{{ title }}</span>
      <span class="intent-execution-status qio-state" :class="statusTone">{{ statusLabel }}</span>
      <span class="intent-execution-toggle">{{ open ? "收起详情" : "查看详情" }}</span>
      <span class="intent-execution-chev" aria-hidden="true">{{ open ? "▴" : "▾" }}</span>
    </button>

    <p
      v-if="verifiedWarning"
      class="intent-execution-verified-note"
      role="status"
      data-test="intent-execution-verified-note"
    >
      {{ verifiedWarning }}
    </p>

    <div
      class="intent-execution-detail"
      :class="{ open }"
      :id="detailId"
      data-test="intent-execution-detail"
      :aria-hidden="open ? 'false' : 'true'"
      :inert="!open"
    >
      <div class="intent-execution-detail-inner">
        <div v-if="scopeBoard || scopeIntents.length || scopeSource" class="intent-sec">
          <h4 class="intent-sec-label">处理范围</h4>
          <p class="intent-scope">
            <span v-if="scopeBoard" class="intent-scope-board">{{ scopeBoard }}</span>
            <span v-if="scopeIntents.length" class="intent-scope-intents">{{ scopeIntents.join("、") }}</span>
            <span v-if="scopeSource" class="intent-scope-source">{{ scopeSource }}</span>
          </p>
        </div>

        <div v-if="attemptRows.length" class="intent-sec">
          <h4 class="intent-sec-label">执行经过</h4>
          <ol class="intent-attempts">
            <li
              v-for="attempt in attemptRows"
              :key="attempt.key"
              class="intent-attempt"
              data-test="intent-attempt"
            >
              <span class="intent-attempt-index mono">第 {{ attempt.index }} 次</span>
              <span v-if="attempt.source" class="intent-attempt-source">{{ attempt.source }}</span>
              <span v-if="attempt.status" class="intent-attempt-status">{{ attempt.status }}</span>
              <span v-if="attempt.at" class="intent-attempt-time mono">{{ attempt.at }}</span>
              <span v-if="attempt.reason" class="intent-attempt-reason">{{ attempt.reason }}</span>
            </li>
          </ol>
        </div>

        <div v-if="intentRows.length > 1" class="intent-sec">
          <h4 class="intent-sec-label">逐项状态</h4>
          <ul class="intent-items">
            <li
              v-for="intent in intentRows"
              :key="intent.key"
              class="intent-item"
              data-test="intent-item"
            >
              <span class="intent-item-title">{{ intent.title }}</span>
              <span class="intent-item-status qio-state" :class="intent.tone">{{ intent.status }}</span>
              <span v-if="intent.attempts !== null" class="intent-item-attempts mono">
                共 {{ intent.attempts }} 次尝试
              </span>
              <span v-if="intent.failure" class="intent-item-failure">{{ intent.failure }}</span>
              <span v-if="intent.resultLabel" class="intent-item-result">{{ intent.resultLabel }}</span>
            </li>
          </ul>
        </div>

        <div v-if="failureReasons.length" class="intent-sec">
          <h4 class="intent-sec-label">失败原因</h4>
          <p v-for="(reason, index) in failureReasons" :key="index" class="intent-failure">
            {{ reason }}
          </p>
        </div>

        <div v-if="resultEntry" class="intent-sec">
          <h4 class="intent-sec-label">结果入口</h4>
          <a
            v-if="resultHref"
            class="intent-result-link"
            data-test="intent-result-link"
            :href="resultHref"
            target="_blank"
            rel="noopener noreferrer"
            @click.prevent="openResult"
          >
            {{ resultLabel }}
          </a>
          <span v-else class="intent-result-text">
            {{ resultLabel }}
            <span v-if="resultTarget" class="intent-result-target mono">{{ resultTarget }}</span>
          </span>
          <p v-if="resultNotice" class="intent-result-notice" role="status">{{ resultNotice }}</p>
        </div>

        <p v-if="verifiedNote" class="intent-verified-note">{{ verifiedNote }}</p>

        <div v-if="diagnosticsRows.length" class="intent-sec">
          <h4 class="intent-sec-label">诊断信息</h4>
          <dl class="intent-diagnostics" data-test="intent-diagnostics">
            <template v-for="row in diagnosticsRows" :key="row.label">
              <dt>{{ row.label }}</dt>
              <dd class="mono">{{ row.value }}</dd>
            </template>
          </dl>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 与用户消息明确区分：左侧记录卡（不用气泡、不用用户侧玫红规线）。
   视觉沿用现有卡片 token：背景、边框、14px 圆角全部来自全局原语。 */
.intent-execution {
  width: 100%;
  min-width: 0;
  margin: 6px 0;
  padding: 0;
  overflow: clip;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}
.intent-execution[data-status="failed"] {
  border-color: var(--border-danger);
}
.intent-execution[data-status="running"] {
  border-color: var(--border-strong);
}
.intent-execution[data-status="unknown"] {
  border-color: var(--border-subtle);
}
.intent-execution-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
  width: 100%;
  min-width: 0;
  padding: 10px 14px;
  background: none;
  border: 0;
  cursor: pointer;
  text-align: left;
  font: inherit;
  color: inherit;
}
.intent-execution-head:hover {
  background: var(--layer-hover);
}
.intent-execution-head:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: -2px;
  border-radius: var(--qio-ui-radius);
}
.intent-execution-tag {
  flex: none;
}
.intent-execution-title {
  min-width: 0;
  max-width: 100%;
  overflow-wrap: anywhere;
  font-family: var(--serif);
  font-size: var(--fs-base);
  color: var(--text-strong);
}
.intent-execution-status {
  flex: none;
}
.intent-execution-toggle {
  margin-left: auto;
  flex: none;
  color: var(--link);
  font-size: var(--fs-xs);
  white-space: nowrap;
}
.intent-execution-chev {
  flex: none;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.intent-execution-verified-note {
  margin: 0;
  padding: 0 14px 10px;
  color: var(--warning);
  font-size: var(--fs-xs);
  line-height: 1.6;
  overflow-wrap: anywhere;
}
/* 展开/收起用 0fr → 1fr 的真实高度过渡；收起时整块 inert，不会被 Tab 聚焦到隐藏内容 */
.intent-execution-detail {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows var(--mo-2-in) var(--ease-2);
  min-width: 0;
}
.intent-execution-detail.open {
  grid-template-rows: 1fr;
}
.intent-execution-detail-inner {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-width: 0;
  min-height: 0;
  overflow: clip;
  padding: 10px 14px 12px;
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-inset);
  opacity: 0;
  transition: opacity var(--mo-2-in) var(--ease-2);
}
.intent-execution-detail.open .intent-execution-detail-inner {
  opacity: 1;
}
.intent-sec {
  min-width: 0;
}
.intent-sec-label {
  margin: 0 0 2px;
  font-size: var(--fs-xs);
  font-weight: 400;
  letter-spacing: 0.06em;
  color: var(--text-muted);
}
.intent-scope {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 10px;
  margin: 0;
  min-width: 0;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.intent-scope-board {
  color: var(--text-primary);
}
.intent-scope-intents {
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.intent-scope-source {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.intent-attempts,
.intent-items {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.intent-attempt,
.intent-item {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 10px;
  min-width: 0;
  line-height: 1.6;
}
.intent-attempt-index {
  color: var(--text-muted);
  font-size: var(--fs-tech);
  white-space: nowrap;
}
.intent-attempt-source {
  color: var(--text-primary);
}
.intent-attempt-status {
  color: var(--text-secondary);
}
.intent-attempt-time {
  color: var(--text-muted);
  font-size: var(--fs-tech);
  white-space: nowrap;
}
.intent-attempt-reason,
.intent-item-failure {
  flex-basis: 100%;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.intent-item-title {
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.intent-item-status {
  flex: none;
}
.intent-item-attempts,
.intent-item-result {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.intent-failure {
  margin: 0;
  color: var(--text-secondary);
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.intent-result-link {
  color: var(--link);
  text-decoration: underline;
  text-underline-offset: 2px;
  overflow-wrap: anywhere;
}
.intent-result-link:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
  border-radius: 2px;
}
.intent-result-text {
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.intent-result-target {
  margin-left: 6px;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.intent-result-notice,
.intent-verified-note {
  margin: 4px 0 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.intent-diagnostics {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 2px 10px;
  margin: 0;
  min-width: 0;
  font-size: var(--fs-xs);
  line-height: 1.6;
}
.intent-diagnostics dt {
  color: var(--text-muted);
}
.intent-diagnostics dd {
  margin: 0;
  min-width: 0;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
</style>
