<script setup lang="ts">
/**
 * 左侧任务栏的一行（A 维护）。只负责展示与就地展开，取数/动作都交给
 * stores/conversationTasks.ts —— 这样列表、详情、收起入口读的是同一份事实。
 *
 * 三条显示纪律：
 * - 后端原状态原样保留在详情里；列表用契约的展示映射（unknown 不会被说成运行中）；
 * - 只有 supported 且本端已接线的动作才有按钮（不做「点了没反应」的假入口）；
 * - 需要处理的失败/待审批保留置顶，批准/拒绝按钮不在这里（统一走审批弹窗）。
 */
import { computed, ref, watch } from "vue";
import type { TaskAction, TaskDetail, TaskFeedItem } from "../../conversation/contracts";
import {
  TASK_ACTION_LABELS,
  formatTaskClock,
  formatTaskDuration,
  isAttentionItem,
  isDestructiveTaskAction,
  taskActionLabel,
  taskDisplayStatus,
  taskOriginText,
  taskSourceText,
  taskStatusLabel,
  taskTimingText,
  visibleTaskActions,
} from "../../services/conversationTasks";
import { vQioScrollbar } from "../../composables/useQioScrollbars";

const props = defineProps<{
  item: TaskFeedItem;
  expanded: boolean;
  detail: TaskDetail | null;
  detailLoading: boolean;
  detailError: string;
  notice: string;
  retainedNote: string;
  /** 这一行有动作请求在飞（全部动作按钮禁用，防重复提交）。 */
  busy: boolean;
  /** 正在提交的动作 kind（文案显示「正在提交…」）。 */
  busyKind: string;
  highlighted: boolean;
}>();

const emit = defineEmits<{
  toggle: [];
  action: [kind: string];
  openApproval: [];
}>();

const status = computed(() => taskDisplayStatus(props.item));
const statusLabel = computed(() => taskStatusLabel(props.item));
const attention = computed(() => isAttentionItem(props.item));
const sourceText = computed(() => taskSourceText(props.item));
const originText = computed(() => taskOriginText(props.item));
const timingText = computed(() => taskTimingText(props.item));
const detailId = computed(() => "task-detail-" + props.item.id);

const approvalEntry = computed(() => status.value === "waiting_approval");

const allActions = computed(() => visibleTaskActions(props.item.actions));
const readMoreAction = computed(
  () => allActions.value.find((action) => action.kind === "read_more") ?? null,
);
const inlineActions = computed(() =>
  allActions.value.filter(
    (action) => action.kind !== "read_more" && action.kind !== "open_approval",
  ),
);
const showReadMore = computed(
  () => Boolean(readMoreAction.value) || props.item.output.truncated || props.item.output.readMore,
);

const confirmingAbandon = ref(false);

watch(
  () => props.expanded,
  (open) => {
    if (!open) confirmingAbandon.value = false;
  },
);

const facts = computed(() => {
  const rows: { label: string; value: string; mono: boolean }[] = [];
  rows.push({ label: "来源", value: sourceText.value, mono: false });
  rows.push({ label: "归属", value: originText.value, mono: false });
  if (props.item.executor) {
    rows.push({ label: "执行者", value: props.item.executor, mono: false });
  }
  rows.push({
    label: "后端状态",
    value: (props.item.rawStatus || "").trim() || "后端没有提供",
    mono: true,
  });
  rows.push({ label: "任务标识", value: props.item.id, mono: true });
  return rows;
});

const timeFacts = computed(() => {
  const rows: string[] = [];
  const now = new Date();
  const queued = formatTaskClock(props.item.queuedAt, now);
  const started = formatTaskClock(props.item.startedAt, now);
  const finished = formatTaskClock(props.item.finishedAt, now);
  if (queued) rows.push("入队 " + queued);
  if (started) rows.push("开始 " + started);
  if (finished) rows.push("结束 " + finished);
  const duration = formatTaskDuration(props.item.durationMs);
  if (duration) rows.push("耗时 " + duration);
  return rows;
});

function actionText(action: TaskAction): string {
  if (props.busy && props.busyKind === action.kind) return "正在提交…";
  return taskActionLabel(action);
}

function onAction(kind: string): void {
  if (props.busy) return;
  if (isDestructiveTaskAction(kind as never)) {
    confirmingAbandon.value = true;
    return;
  }
  emit("action", kind);
}

function confirmAbandon(): void {
  confirmingAbandon.value = false;
  emit("action", "abandon_dev");
}

function cancelAbandon(): void {
  confirmingAbandon.value = false;
}

function showDetailParts(): boolean {
  return Boolean(props.detail && (props.detail.parts.length || props.detail.outputMissing));
}

function partKind(kind: string): string {
  return "kind-" + kind;
}

function fallbackActionLabel(kind: string): string {
  return TASK_ACTION_LABELS[kind as keyof typeof TASK_ACTION_LABELS] ?? "操作";
}
</script>

<template>
  <article
    class="task-item"
    :class="{ 'is-attention': attention, 'is-expanded': expanded, highlighted }"
    :data-status="status"
    :data-attention="attention ? 'true' : 'false'"
  >
    <div class="task-line">
      <span class="status-dot" aria-hidden="true"></span>
      <button
        class="task-trigger"
        type="button"
        :aria-expanded="expanded"
        :aria-controls="detailId"
        @click="emit('toggle')"
      >
        <span class="task-title">{{ item.title || item.summary || "（这项任务没有标题）" }}</span>
        <svg class="chevron" :class="{ rotated: expanded }" viewBox="0 0 16 16" aria-hidden="true">
          <path d="M5.5 6.5 8 9l2.5-2.5" />
        </svg>
      </button>
    </div>

    <div class="task-meta">
      <span class="task-status" :data-status="status">{{ statusLabel }}</span>
      <span v-if="attention" class="task-need">需要处理</span>
      <span class="task-source">{{ sourceText }}</span>
      <span class="task-origin">{{ originText }}</span>
      <span v-if="timingText" class="task-time mono">{{ timingText }}</span>
    </div>

    <button
      v-if="approvalEntry"
      class="task-approval-entry"
      type="button"
      @click="emit('openApproval')"
    >
      查看审批
    </button>

    <div v-if="expanded" :id="detailId" class="task-detail">
      <p v-if="item.summary && item.summary !== item.title" class="task-summary">
        {{ item.summary }}
      </p>

      <dl class="task-facts">
        <template v-for="row in facts" :key="row.label">
          <dt>{{ row.label }}</dt>
          <dd :class="{ mono: row.mono }">{{ row.value }}</dd>
        </template>
      </dl>

      <p v-if="timeFacts.length" class="task-times mono">{{ timeFacts.join(" · ") }}</p>

      <p v-if="retainedNote" class="task-retained-note" role="status">{{ retainedNote }}</p>

      <div v-if="item.output.preview || item.output.hint" class="task-output">
        <div class="task-output-head">
          <span>输出</span>
          <span v-if="item.output.truncated" class="task-output-flag mono">仅显示部分</span>
        </div>
        <pre v-if="item.output.preview" v-qio-scrollbar class="task-output-preview">{{ item.output.preview }}</pre>
        <p v-if="item.output.hint" class="task-output-hint">{{ item.output.hint }}</p>
      </div>

      <button
        v-if="showReadMore"
        class="task-action"
        type="button"
        :disabled="busy || detailLoading"
        :aria-busy="detailLoading ? 'true' : 'false'"
        @click="emit('action', 'read_more')"
      >
        {{ detailLoading ? "正在读取完整内容…" : "查看更多" }}
      </button>
      <p v-if="detailError" class="task-error" role="alert">{{ detailError }}</p>

      <div v-if="showDetailParts()" class="task-detail-parts">
        <p v-if="detail && detail.outputMissing" class="task-output-hint">
          完整输出现在不可用：{{ detail.missingReason || "记录已被清理，只能看到保留的信息。" }}
        </p>
        <section
          v-for="(part, index) in detail ? detail.parts : []"
          :key="index"
          class="task-part"
        >
          <p class="task-part-label mono">
            {{ part.label }}
            <span v-if="part.truncated" class="task-output-flag">仅显示部分</span>
          </p>
          <pre v-qio-scrollbar class="task-part-text" :class="partKind(part.kind)">{{ part.text }}</pre>
        </section>
      </div>

      <div v-if="inlineActions.length" class="task-actions">
        <button
          v-for="action in inlineActions"
          :key="action.kind"
          class="task-action"
          :class="{ 'is-danger': action.kind === 'abandon_dev' || action.kind === 'cancel' }"
          type="button"
          :disabled="busy"
          :aria-busy="busy && busyKind === action.kind ? 'true' : 'false'"
          @click="onAction(action.kind)"
        >
          {{ actionText(action) }}
        </button>
      </div>

      <p v-if="notice" class="task-notice" role="status">{{ notice }}</p>

      <div v-if="confirmingAbandon" class="task-confirm">
        <p class="task-confirm-text">
          结束这项开发并从未完成列表移除；工作区文件与记录保留，不删除已注册的工具；
          同时收回这个任务的执行授权，没回答的确认会失效。
        </p>
        <div class="task-confirm-actions">
          <button class="task-action is-danger" type="button" @click="confirmAbandon">
            {{ fallbackActionLabel("abandon_dev") }}
          </button>
          <button class="task-action" type="button" @click="cancelAbandon">取消</button>
        </div>
      </div>
    </div>
  </article>
</template>

<style scoped>
.task-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 9px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-base);
  color: var(--text-primary);
  min-width: 0;
  overflow-wrap: anywhere;
  transition: border-color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.task-item:hover {
  border-color: var(--border-strong);
}
.task-item.is-attention {
  border-color: var(--border-strong);
  background: var(--bg-surface);
}
.task-item.is-expanded {
  background: var(--bg-surface);
}
.task-item.highlighted {
  animation: task-highlight 1.6s var(--ease-1) both;
}
@keyframes task-highlight {
  0% { background: var(--accent-soft); }
  60% { background: var(--accent-soft); }
  100% { background: transparent; }
}
.task-line {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  min-width: 0;
}
.status-dot {
  flex: none;
  width: 6px;
  height: 6px;
  margin-top: 6px;
  border-radius: 50%;
  background: var(--text-faint);
}
.task-item[data-status="running"] .status-dot {
  background: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.task-item[data-status="queued"] .status-dot { background: var(--text-muted); }
.task-item[data-status="waiting_approval"] .status-dot,
.task-item[data-status="waiting_user"] .status-dot { background: var(--warning); }
.task-item[data-status="completed"] .status-dot { background: var(--success); }
.task-item[data-status="partial"] .status-dot { background: var(--warning); }
.task-item[data-status="failed"] .status-dot { background: var(--danger); }
.task-item[data-status="cancelled"] .status-dot,
.task-item[data-status="budget_exhausted"] .status-dot,
.task-item[data-status="unknown"] .status-dot { background: var(--text-faint); }

.task-trigger {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
  font-family: inherit;
}
.task-trigger:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
  border-radius: var(--r-xs);
}
.task-title {
  flex: 1;
  min-width: 0;
  font-size: var(--fs-sm);
  line-height: 1.5;
  color: var(--text-strong);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  overflow-wrap: anywhere;
}
.chevron {
  flex: none;
  width: 14px;
  height: 14px;
  margin-top: 2px;
  fill: none;
  stroke: var(--text-muted);
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform var(--dur-fast) var(--ease-1);
}
.chevron.rotated { transform: rotate(180deg); }

.task-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
  padding-left: 13px;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  min-width: 0;
}
.task-status {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}
.task-status[data-status="running"] { color: var(--accent); }
.task-status[data-status="waiting_approval"],
.task-status[data-status="waiting_user"],
.task-status[data-status="partial"],
.task-status[data-status="budget_exhausted"] { color: var(--warning); }
.task-status[data-status="failed"] { color: var(--danger); }
.task-status[data-status="completed"] { color: var(--success); }
.task-status[data-status="unknown"] { color: var(--text-muted); }
.task-need {
  color: var(--warning);
  border: 1px solid var(--warning-soft);
  background: var(--warning-soft);
  border-radius: var(--r-pill);
  padding: 0 6px;
  line-height: 1.6;
}
.task-source {
  color: var(--text-secondary);
}
.task-origin,
.task-time {
  color: var(--text-muted);
  min-width: 0;
  overflow-wrap: anywhere;
}
.task-time {
  font-size: 10.5px;
}

.task-approval-entry {
  align-self: flex-start;
  margin-left: 13px;
  padding: 3px 9px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  background: transparent;
  color: var(--warning);
  font-size: var(--fs-xs);
  font-family: inherit;
  cursor: pointer;
}
.task-approval-entry:hover {
  border-color: var(--warning);
}
.task-approval-entry:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}

.task-detail {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px 0 2px 13px;
  border-top: 1px solid var(--border-subtle);
  margin-top: 2px;
}
.task-summary {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: 1.7;
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.task-facts {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 3px 10px;
  margin: 0;
  font-size: var(--fs-xs);
}
.task-facts dt {
  color: var(--text-muted);
  white-space: nowrap;
}
.task-facts dd {
  margin: 0;
  color: var(--text-secondary);
  min-width: 0;
  overflow-wrap: anywhere;
}
.task-times {
  margin: 0;
  font-size: 10.5px;
  color: var(--text-muted);
  overflow-wrap: anywhere;
}
.task-retained-note {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--warning);
  line-height: 1.6;
}
.task-output {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.task-output-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.task-output-flag {
  color: var(--warning);
  font-size: 10px;
}
.task-output-preview {
  margin: 0;
  padding: 7px 9px;
  max-height: 220px;
  overflow: auto;
  overscroll-behavior: contain;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--bg-inset);
  color: var(--text-secondary);
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.65;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.task-output-hint {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.task-detail-parts {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.task-part {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}
.task-part-label {
  margin: 0;
  font-size: 10.5px;
  color: var(--text-muted);
}
.task-part-text {
  margin: 0;
  padding: 7px 9px;
  max-height: 300px;
  overflow: auto;
  overscroll-behavior: contain;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--bg-inset);
  color: var(--text-secondary);
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.65;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.task-part-text.kind-error { color: var(--danger); }
.task-part-text.kind-note { color: var(--text-muted); }
.task-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}
.task-action {
  padding: 4px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  background: transparent;
  color: var(--text-secondary);
  font-family: inherit;
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease), border-color var(--dur-fast) var(--ease);
}
.task-action:hover {
  color: var(--text-strong);
  border-color: var(--border-strong);
}
.task-action.is-danger:hover {
  color: var(--danger);
  border-color: var(--danger);
}
.task-action:disabled {
  opacity: 0.55;
  cursor: default;
}
.task-action:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.task-notice {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.task-error {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--danger);
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.task-confirm {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 9px;
  border: 1px solid var(--border-danger);
  border-radius: var(--r-sm);
  background: var(--danger-soft);
}
.task-confirm-text {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.7;
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.task-confirm-actions {
  display: flex;
  gap: 6px;
}
</style>
