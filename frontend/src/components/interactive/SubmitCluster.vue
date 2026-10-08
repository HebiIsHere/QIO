<!--
  提交区（子智能体 C 负责实现）：放在底部工具栏右端，由 A 在 BoardToolbar 里渲染。

  契约：docs/interactive-mode-contract.md §8.1 / §8.4，以及 §1.5 的保存 / 提交分离。

  这里必须说准四件事，不许含糊：
  1. 保存状态与提交状态分开说：**「已保存」不等于「QIO 已收到」**，
     只有提交成功才更新基准；点击按钮或请求刚发出都不算成功。
  2. 提交按钮只调 store.submit()（保存 → 服务端求差 → 提交），提交中禁用并显示「正在提交…」。
  3. 「本次有效改动」与「本次允许 QIO 查看的范围」都能看到：
     visible-range 始终可见（自动化与用户都不必先展开），改动列表按需展开。
  4. 失败保留改动与勾选、说出真实原因，并允许直接重试（不假装成功、不清空板面）。
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { fetchBoardState } from "../../services/interactive";
import type { BoardStateResponse, Expression } from "../../interactive/types";
import {
  draftHintText,
  localVisibleRange,
  saveStateText,
  submitFailureBrief,
  submitStateLabel,
  submitStatusText,
  summarizeChanges,
  visibleRangeCounts,
  visibleRangeText,
} from "../../interactive/submission";
import BoardChangeList from "./BoardChangeList.vue";

const store = useInteractiveStore();
const detailsOpen = ref(false);
const busy = computed(() => store.submitStatus === "submitting");

/**
 * 未提交的有效改动由**服务端**在每次保存后算好（board_pending），这里只读取展示；
 * 不在前端做第二套求差逻辑，免得两边语义漂移。
 */
const pendingExpressions = ref<Expression[]>([]);
const pendingError = ref<string | null>(null);
let fetchSeq = 0;

async function refreshPending() {
  if (!store.board) {
    pendingExpressions.value = [];
    return;
  }
  const seq = ++fetchSeq;
  try {
    const payload = (await fetchBoardState(store.boardId)) as BoardStateResponse & {
      pending?: { expressions?: Expression[]; error?: string };
    };
    if (seq !== fetchSeq) return;
    pendingExpressions.value = payload.pending?.expressions ?? [];
    pendingError.value = payload.pending?.error ?? null;
  } catch (err) {
    if (seq !== fetchSeq) return;
    pendingExpressions.value = [];
    pendingError.value = (err as Error).message;
  }
}

onMounted(() => {
  void refreshPending();
});

// 载入完成、保存完成、提交结束后各刷新一次预览（提交与保存由 store 负责，这里只读）
watch(
  () => [store.loading, store.saveStatus, store.submitStatus] as const,
  ([loading]) => {
    if (!loading) void refreshPending();
  },
);

const summary = computed(() => summarizeChanges(pendingExpressions.value));
/** 保存状态 + 「已保存 ≠ QIO 已收到」这条边界，合成一句话（顶部状态行是 save-status 的唯一持有者） */
const saveText = computed(
  () =>
    `${saveStateText(store.saveStatus, store.dirty, store.lastSavedAt)}。已保存 ≠ QIO 已收到；只有提交成功才会更新基准。`,
);
const stateLabel = computed(() =>
  submitStateLabel(store.submitStatus, {
    pendingCount: pendingExpressions.value.length,
    hasSubmission: Boolean(store.lastSubmission),
  }),
);
/**
 * 失败说明（契约 §11.6）：原因、保留情况、下一步全部取**本次请求**的事实。
 *
 * 上一次提交结果只作为详情里的排查信息；它绝不能冒充本次失败原因 ——
 * 那正是上一轮「默认失败区显示的是上一次结果」的缺陷。
 */
const failure = computed(() =>
  submitFailureBrief({
    status: store.submitStatus,
    error: store.submitError,
    saveStatus: store.saveStatus,
    saveError: store.saveError,
    dirty: store.dirty,
    lastResult: store.lastSubmission,
  }),
);
const statusText = computed(() =>
  submitStatusText(store.submitStatus, {
    result: store.lastSubmission,
    pendingCount: pendingExpressions.value.length,
    error: failure.value?.reason ?? store.submitError,
    retention: failure.value?.retention,
  }),
);
const range = computed(() => localVisibleRange(store.board));
const rangeText = computed(() => visibleRangeText(range.value));
/**
 * 常态只留一行**简短**范围（契约 §10.8）：完整排除清单与说明放进「详情」，
 * 不再靠父组件 :deep() 压缩多段文字来维持高度。
 */
const rangeShort = computed(() => {
  const counts = visibleRangeCounts(range.value);
  if (!range.value) return "本次可见：还没有可提交的内容";
  return `本次可见：材料 ${counts.materials} 项 · 注释 ${counts.notes} 条`;
});
/**
 * 是不是**本次**失败：只看当前请求的状态。
 * 不再看 lastSubmission —— 上一次的失败结果不该在成功之后继续显示成失败。
 */
const isFailed = computed(() => failure.value !== null);
const draftText = draftHintText();
/** 提交按钮文案：失败后明确写成「重新提交」，让重试一眼可见 */
const submitLabel = computed(() => {
  if (busy.value) return "正在提交…";
  if (isFailed.value) return "重新提交";
  return "提交给 QIO";
});

function onSubmit() {
  void store.submit();
}
</script>

<template>
  <!--
    常态只有三样东西（契约 §10.8）：一行简短范围、一行简短提交状态、提交按钮（外加「详情」）。
    完整排除清单、改动列表、保存与提交的边界说明、草稿提示都收进按需打开的详情里。
  -->
  <div class="submit-cluster" data-im="submit-cluster" :data-im-failed="isFailed ? '1' : undefined">
    <p class="range" data-im="visible-range">{{ rangeShort }}</p>

    <p class="status" data-im="submit-status" role="status">
      <strong class="label" :class="{ ok: store.submitStatus === 'succeeded', warn: isFailed }">
        {{ stateLabel }}
      </strong>
      <!-- 常态只说「几项改动」；完整摘要与边界说明在「详情」里（§10.8） -->
      <span class="detail">{{ summary.hasContent ? summary.total + " 项改动" : "没有可提交的改动" }}</span>
    </p>

    <!--
      失败必须立刻可见（真实错误不许藏进详情）：默认就说清「本次原因 → 保留情况 → 重试」。
      顺序固定，详情只补充较长诊断（保存失败原因、上一次提交返回的说明），不是了解原因的必经入口。
    -->
    <p v-if="failure" class="failure" role="alert" data-im="submit-failure">
      <span class="failure-reason" data-im="submit-failure-reason">失败原因：{{ failure.reason }}</span>
      <span class="failure-retention" data-im="submit-failure-retention">{{ failure.retention }}</span>
    </p>

    <div class="actions">
      <button
        class="details"
        type="button"
        data-im="submit-details"
        :aria-expanded="detailsOpen"
        @click="detailsOpen = !detailsOpen"
      >
        {{ detailsOpen ? "收起详情" : "详情" }}
      </button>
      <button
        class="submit"
        type="button"
        data-im="submit"
        :title="failure?.nextStep"
        :disabled="busy || !store.board"
        @click="onSubmit"
      >
        {{ submitLabel }}
      </button>
    </div>

    <section v-if="detailsOpen" class="details-box" data-im="submit-details-box">
      <p class="line" data-im="submit-save-status" role="status">{{ saveText }}</p>
      <p class="line range" data-im="visible-range-full">{{ rangeText }}</p>
      <p class="line strong">{{ statusText }}</p>
      <p v-if="failure" class="line err" data-im="submit-failure-detail">{{ failure.detail }}</p>
      <BoardChangeList
        :expressions="pendingExpressions"
        title="本次有效改动（尚未提交）"
        empty-text="本次还没有可提交的改动（服务端在每次保存后重算）。"
      />
      <p v-if="pendingError" class="line err" role="status">
        未提交改动预览失败：{{ pendingError }}（提交时服务端仍会重新求差，不影响提交）
      </p>
      <p class="line faint">{{ draftText }}</p>
    </section>
  </div>
</template>

<style scoped>
.submit-cluster {
  position: relative;
  display: flex;
  align-items: center;
  /* 允许换行：失败区可以换行、可以变高，但按钮行不会被挤坏（.actions 不参与收缩） */
  flex-wrap: wrap;
  row-gap: var(--sp-1);
  gap: var(--sp-3);
  min-width: 0;
  padding-left: var(--sp-4);
  /* 与编辑操作留出间距并区分样式（契约 §8.1） */
  border-left: 1px solid var(--border-strong);
  background: transparent;
  font-family: var(--sans);
}
/* 常态排成一行：简短范围 + 简短状态 + 操作（契约 §10.8），窄窗口也尽量不竖排 */
.range,
.status {
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.range { flex: 0 1 auto; }
.status { flex: 1 1 auto; }
.line {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.45;
  color: var(--text-muted);
}
.line.strong {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  min-width: 0;
}
.label {
  flex: none;
  font-family: var(--mono);
  font-size: var(--fs-xs);
  color: var(--text-primary);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-1);
}
.label.ok { color: var(--success); border-color: var(--success); }
.label.warn { color: var(--danger); border-color: var(--danger); }
.detail {
  min-width: 0;
  color: var(--text-secondary);
}
/* 允许查看范围始终可见（不折叠才存在）；视觉上最多两行，全文仍在 DOM 里 */
.line.range {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
/*
  失败区（契约 §11.6 / §11.8）：默认就给出「本次原因 + 保留情况」，各占一行、顺序固定。
  长原因允许换行（overflow-wrap），但整块有高度上限、超出时区内滚动，
  所以既不会撑坏右边的提交按钮，也不会把板面盖住；颜色走令牌，明暗两主题都成立。
*/
.failure {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 0;
  padding: var(--sp-1) var(--sp-2);
  max-height: calc(var(--fs-xs) * 1.45 * 4);
  overflow-y: auto;
  overflow-wrap: anywhere;
  border-left: 2px solid var(--danger);
  border-radius: var(--r-xs);
  background: var(--danger-soft);
  font-size: var(--fs-xs);
  line-height: 1.45;
}
.failure-reason { color: var(--danger); }
.failure-retention { color: var(--text-secondary); }
.line.retain { color: var(--warning); }
.line.err { color: var(--danger); }
.line.faint { color: var(--text-faint); }
.actions {
  flex: none;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.count {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.details {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 2px var(--sp-2);
  cursor: pointer;
}
.details:hover { color: var(--text-strong); border-color: var(--border-strong); }
.details:focus-visible { outline: 2px solid var(--link); outline-offset: 1px; }
.submit {
  font: inherit;
  font-size: var(--fs-base);
  color: var(--on-accent);
  background: var(--accent);
  border: none;
  border-radius: var(--r-sm);
  padding: var(--sp-2) var(--sp-5);
  cursor: pointer;
}
.submit:hover:not(:disabled) { background: var(--accent-hover); }
.submit:disabled { opacity: 0.55; cursor: default; }
.submit:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
/* 展开区从提交区上方长出来，不把工具栏顶变形 */
.details-box {
  position: absolute;
  right: 0;
  bottom: calc(100% + var(--sp-2));
  z-index: 30;
  width: min(460px, 88vw);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  box-shadow: var(--shadow-2);
}
@media (max-width: 900px) {
  /* 窄窗口仍排成一行；实在放不下才换行（换行也不会超过两行，工具栏总高目标 ≤96px 仍满足） */
  .submit-cluster {
    flex-wrap: wrap;
    row-gap: var(--sp-1);
    padding-left: 0;
    border-left: none;
  }
  .texts { max-width: none; }
  .actions { justify-content: flex-end; }
  .details-box { width: min(420px, 92vw); }
}
</style>