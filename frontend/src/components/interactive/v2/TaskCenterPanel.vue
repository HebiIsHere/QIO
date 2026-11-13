<!--
  F03：互动任务面板（A 组）。

  基线缺陷：HUD 的「任务」按钮只切 store.tasksOpen，承接它的旧任务浮层未挂载；
  提交区的「打开批量审批」只切旧的批量开关（batchOpen，已删除），旧批量托盘也未挂载 ——
  两个入口点了没有任何可见反应，运行中的任务也没有暂停 / 取消 / 继续入口。
  现在两条入口都落在这一个面板上（tasksOpen 唯一开合状态，批量入口带 focus="pending"）；
  面板另提供「审批结果与历史」分区（A 的只读面板），收起对话时也能从这里进结果。

  这一份界面同时承接两条入口（「任务」与「批量待确认」），内容与操作资格都来自
  store.taskRows（唯一一份任务行）：不再有「两份互不更新的列表分别驱动界面和资格」。
  - 暂停 / 继续 / 取消走既有执行控制接口（store.pauseExecution / resumeExecution / cancelExecution）；
  - 文案明确区分「取消这条互动任务」与「停止当前聊天」；
  - 批准 / 拒绝只对旧链存储里的待处理意图出现（新链确认在板面的虚拟意图卡上，不重复）；
  - 每个操作有进行中状态与失败反馈，成功后由 store 刷新任务清单与执行记录。
-->
<script setup lang="ts">
import { vQioTip } from "../../../composables/useQioTip";
import { computed, onMounted, ref } from "vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { useApprovalResultsStore } from "../../../stores/approvalResults";
import ApprovalResultsPanel from "../../approval/ApprovalResultsPanel.vue";
import QIcon from "../../ui/QIcon.vue";
import QIconButton from "../../ui/QIconButton.vue";
import {
  batchAreaVisible,
  batchDecideIds,
  taskControlHint,
  taskControlLabel,
  taskActionIcon,
  taskCountsSummary,
  taskEmptyText,
  taskReadFailures,
  taskStatusIcon,
  type TaskControlAction,
  type TaskReadFailure,
  type TaskRow,
} from "./taskCenter";

const props = withDefaults(
  defineProps<{
    /** 从哪条入口打开：批量入口直接落到「待确认」分区。 */
    focus?: "tasks" | "pending";
  }>(),
  { focus: "tasks" },
);

const emit = defineEmits<{
  (e: "close"): void;
  /** 打开某条意图的执行记录（由页面按 id 找到最新视图后打开面板） */
  (e: "open-record", intentId: string): void;
}>();

const store = useInteractiveStore();
/** #56：审批历史分区的刷新与计数都走历史查询自己的 store（不借任务读取的状态）。 */
const approvalResults = useApprovalResultsStore();
const busy = ref("");
const notice = ref("");
const selected = ref<string[]>([]);
/**
 * 面板分区（F05 / 审批结果可达性）：任务操作与只读的审批结果/历史分开，不混在一列。
 * 默认是任务分区；HUD「任务」常驻，所以对话收起时也能通过「任务 → 审批结果与历史」走到结果。
 * 面板每次关闭即卸载，再打开回到默认分区（不做跨会话记忆）。
 */
const tab = ref<"tasks" | "approval-results">("tasks");

const rows = computed<TaskRow[]>(() => store.taskRows);
const liveRows = computed(() => rows.value.filter((row) => row.bucket === "live"));
const pausedRows = computed(() => rows.value.filter((row) => row.bucket === "paused"));
const waitingRows = computed(() => rows.value.filter((row) => row.bucket === "waiting"));
const finishedRows = computed(() => rows.value.filter((row) => row.bucket === "finished"));
/** 旧链存储里的待处理行：批准 / 拒绝接口对它们有效（拒绝能力在这里保留） */
const legacyPending = computed(() => waitingRows.value.filter((row) => row.rejectable && !row.fromView));
const batchIds = computed(() => batchDecideIds(rows.value, selected.value));

const counts = computed(() => store.taskCounts);
/** #18：有任务时只显示有内容的组及计数（全零由空态一句话表达）。 */
const countsSummary = computed(() => taskCountsSummary(counts.value));
/**
 * #74：批量区只在「进入批量选择」（批量入口打开，focus="pending"）且存在
 * 可处理的待确认项时出现；零项收起，不在普通任务视图里常驻。
 */
const batchMode = computed(() => props.focus === "pending");
const batchEligible = computed(() => legacyPending.value);
const showBatch = computed(() => batchAreaVisible(props.focus, batchEligible.value.length));
/**
 * #07：同一次任务读取失败只给一处可重试错误；已有数据标「上次结果」，
 * 首次失败标「读取失败」；任务清单 / 旧任务记录 / 执行状态各自独立保留。
 *
 * executions 的读取状态优先用 store 的 executionsLoadState（i-state 本轮新增）；
 * 尚未合并时退回既有 executionError + 是否有历史执行记录。
 */
const readFailures = computed<TaskReadFailure[]>(() => {
  const storeFacts = store as unknown as {
    executionsLoadState?: "idle" | "loading" | "ok" | "error";
    executionsLoadedOnce?: boolean;
    executionsLoadError?: string | null;
  };
  const execError = storeFacts.executionsLoadError ?? store.executionError ?? null;
  const execHasData = storeFacts.executionsLoadedOnce === true || (store.executions?.length ?? 0) > 0;
  return taskReadFailures({
    views: {
      state: store.intentViewsLoadState,
      loadedOnce: store.intentViewsLoadedOnce,
      error: store.intentViewsLoadError,
    },
    legacy: {
      state: store.intentsLoadState,
      loadedOnce: store.intentsLoadedOnce,
      error: store.intentsLoadError,
    },
    executions: { error: execError, hasData: execHasData },
  });
});
/** 视图读取失败时不显示「暂无任务」：读不到不等于没有任务。 */
const emptyVisible = computed(
  () => !rows.value.length && !readFailures.value.some((failure) => failure.key === "views"),
);

/** #18：空任务只有一句话「暂无任务」（分区入口决定看哪一类，不用不同空态文案区分）。 */
const emptyText = computed(() => taskEmptyText());

function toggleSelected(intentId: string): void {
  selected.value = selected.value.includes(intentId)
    ? selected.value.filter((id) => id !== intentId)
    : [...selected.value, intentId];
}

/** #56：刷新**当前分区**——任务分区刷新任务事实，审批历史分区刷新历史查询。 */
async function refresh(): Promise<void> {
  if (tab.value === "approval-results") {
    await approvalResults.loadHistory();
    return;
  }
  await store.refreshAfterIntentAction();
}

/** #07：只重试失败的那一条读取（不连带其它动作，不清旧数据）。 */
async function retryRead(failure: TaskReadFailure): Promise<void> {
  if (busy.value) return;
  busy.value = "read:" + failure.key;
  notice.value = "";
  try {
    if (failure.key === "views") await store.loadIntentViews();
    else if (failure.key === "legacy") await store.loadIntents();
    else await store.loadExecutions();
  } finally {
    busy.value = "";
  }
}

/** #56：分区标签上的刷新文案只描述这一分区在做的事。 */
const refreshLabel = computed(() =>
  tab.value === "approval-results" ? "刷新审批历史" : "刷新任务清单",
);

async function runControl(row: TaskRow, action: TaskControlAction): Promise<void> {
  const key = row.intentId + ":" + action;
  if (busy.value) return;
  busy.value = key;
  notice.value = "";
  try {
    const result =
      action === "pause"
        ? await store.pauseExecution(row.intentId)
        : action === "resume"
          ? await store.resumeExecution(row.intentId)
          : await store.cancelExecution(row.intentId);
    if (!result?.ok) {
      notice.value = "这次操作没有生效：" + (result?.reason || "服务端没有给出原因");
      return;
    }
    notice.value =
      (action === "pause" ? "已暂停：" : action === "resume" ? "已继续：" : "已取消：") +
      row.title +
      "（状态以服务端执行记录为准）";
  } catch (err) {
    notice.value = "这次操作没有完成：" + ((err as Error)?.message || "原因未知");
  } finally {
    busy.value = "";
  }
}

async function decide(row: TaskRow, decision: "approve" | "reject"): Promise<void> {
  const key = row.intentId + ":" + decision;
  if (busy.value) return;
  busy.value = key;
  notice.value = "";
  try {
    const result =
      decision === "approve" ? await store.approve(row.intentId, false) : await store.reject(row.intentId);
    if (!result?.ok) {
      notice.value = "没有" + (decision === "approve" ? "批准" : "拒绝") + "：" +
        (result?.detail ?? result?.reason ?? "服务端未说明原因");
      return;
    }
    notice.value =
      decision === "approve"
        ? "已批准：" + row.title + "（执行状态以服务端执行记录为准，不等于已完成）"
        : "已拒绝：" + row.title;
    selected.value = selected.value.filter((id) => id !== row.intentId);
  } catch (err) {
    notice.value = "这次决定没有完成：" + ((err as Error)?.message || "原因未知");
  } finally {
    busy.value = "";
  }
}

async function decideBatch(decision: "approve" | "reject"): Promise<void> {
  if (busy.value) return;
  const ids = batchIds.value;
  if (ids.length < 2) {
    notice.value = "同批操作至少选两条；只处理一条时请用该行自己的按钮。";
    return;
  }
  busy.value = "batch:" + decision;
  notice.value = "";
  try {
    const result =
      decision === "approve" ? await store.decideBatch(ids, []) : await store.decideBatch([], ids);
    const error = (result as { error?: string } | null)?.error;
    if (error) {
      notice.value = "同批操作没有完成：" + error;
      return;
    }
    notice.value =
      "同批" + (decision === "approve" ? "批准" : "拒绝") + "已提交 " + ids.length + " 条（结果以服务端回执为准）";
    selected.value = [];
  } catch (err) {
    notice.value = "同批操作没有完成：" + ((err as Error)?.message || "原因未知");
  } finally {
    busy.value = "";
  }
}

function openRecord(row: TaskRow): void {
  emit("open-record", row.intentId);
}

onMounted(() => {
  void refresh();
});
</script>

<template>
  <section
    class="tc"
    data-im="task-center"
    role="dialog"
    aria-modal="false"
    aria-label="互动任务"
    :data-focus="props.focus"
  >
    <header class="tc-head">
      <h2 class="tc-title">互动任务</h2>
      <!-- #18：只报有内容的组；全零由下方空态一句话表达 -->
      <span v-if="countsSummary" class="tc-counts mono" data-im="task-counts">{{ countsSummary }}</span>
      <!-- #84：刷新 / 关闭复用全站唯一的共享图标按钮 -->
      <QIconButton
        icon="refresh"
        :label="refreshLabel"
        size="sm"
        tone="quiet"
        data-im="task-refresh"
        @click="refresh"
      />
      <QIconButton
        icon="close"
        label="关闭任务面板"
        size="sm"
        tone="quiet"
        data-im="task-close"
        @click="emit('close')"
      />
    </header>

    <!-- 分区：任务（可操作） / 审批结果与历史（只读）。两者不混在同一列操作里。 -->
    <div class="tc-tabs" role="tablist" aria-label="任务面板分区">
      <button
        class="tc-tab"
        type="button"
        role="tab"
        data-im="task-tab-tasks"
        :aria-selected="tab === 'tasks' ? 'true' : 'false'"
        :data-active="tab === 'tasks' ? '1' : '0'"
        @click="tab = 'tasks'"
      >任务</button>
      <button
        class="tc-tab"
        type="button"
        role="tab"
        data-im="task-tab-approval-results"
        aria-controls="tc-approval-results"
        :aria-selected="tab === 'approval-results' ? 'true' : 'false'"
        :data-active="tab === 'approval-results' ? '1' : '0'"
        @click="tab = 'approval-results'"
      >审批结果与历史</button>
    </div>

    <!-- 审批结果与历史：A 的只读面板（零 props；自己读结果/历史，含恢复失败与读取失败重试）。
         这里只给容器与滚动，不复制任何面板逻辑，也不新增第二个浮动入口。 -->
    <div
      v-if="tab === 'approval-results'"
      id="tc-approval-results"
      class="tc-results"
      role="tabpanel"
      aria-label="审批结果与历史"
      data-im="task-approval-results"
    >
      <ApprovalResultsPanel />
    </div>

    <template v-if="tab === 'tasks'">
    <!-- #07：每条独立失败只在任务面板给一处可重试错误；已有数据标「上次结果」，首次失败标「读取失败」。 -->
    <div
      v-for="failure in readFailures"
      :key="failure.key"
      class="tc-warn tc-read-error"
      role="alert"
      data-im="task-read-error"
      :data-failure="failure.key"
      :data-marker="failure.marker"
    >
      <span class="tc-read-head">
        <QIcon :name="failure.firstFailure ? 'warning' : 'info'" :size="12" aria-hidden="true" />
        <strong data-im="task-read-marker">{{ failure.marker }}</strong>
        <span>{{ failure.title }}：{{ failure.detail }}</span>
      </span>
      <button
        class="tc-link"
        type="button"
        data-im="task-read-retry"
        :data-failure="failure.key"
        :disabled="Boolean(busy)"
        @click="retryRead(failure)"
      >{{ failure.retryLabel }}</button>
    </div>

    <p v-if="notice" class="tc-notice" role="status" data-im="task-notice">{{ notice }}</p>

    <p v-if="emptyVisible" class="tc-empty" data-im="task-empty">{{ emptyText }}</p>

    <template v-else>
      <section v-if="liveRows.length" class="tc-block" data-im="task-live">
        <h3 class="tc-block-title">执行中（{{ liveRows.length }}）</h3>
        <ul class="tc-list">
          <li
            v-for="row in liveRows"
            :key="row.intentId"
            class="tc-row"
            data-im="task-row"
            :data-intent-id="row.intentId"
            :data-status="row.status"
            :data-execution="row.executionStatus"
          >
            <span class="tc-name">{{ row.title }}</span>
            <span class="tc-status mono" :data-tone="row.tone" data-im="task-status">
              <QIcon :name="taskStatusIcon(row)" :size="12" aria-hidden="true" />
              {{ row.statusText }}
            </span>
            <span class="tc-actions" data-im="task-actions">
              <button
                v-for="action in row.actions"
                :key="action"
                class="tc-btn"
                type="button"
                :data-im="'task-' + action"
                :data-intent-id="row.intentId"
                v-qio-tip="taskControlHint(action)"
                :disabled="Boolean(busy)"
                @click="runControl(row, action)"
              ><QIcon :name="taskActionIcon(action)" :size="11" aria-hidden="true" />{{ taskControlLabel(action, busy === row.intentId + ':' + action) }}</button>
              <button class="tc-btn ghost" type="button" data-im="task-record" @click="openRecord(row)"><QIcon name="clock" :size="11" aria-hidden="true" />执行记录</button>
            </span>
          </li>
        </ul>
      </section>

      <section v-if="pausedRows.length" class="tc-block" data-im="task-paused">
        <h3 class="tc-block-title">已暂停（{{ pausedRows.length }}）</h3>
        <ul class="tc-list">
          <li
            v-for="row in pausedRows"
            :key="row.intentId"
            class="tc-row"
            data-im="task-row"
            :data-intent-id="row.intentId"
            :data-status="row.status"
            :data-execution="row.executionStatus"
          >
            <span class="tc-name">{{ row.title }}</span>
            <span class="tc-status mono" :data-tone="row.tone" data-im="task-status">
              <QIcon :name="taskStatusIcon(row)" :size="12" aria-hidden="true" />
              {{ row.statusText }}
            </span>
            <span class="tc-actions" data-im="task-actions">
              <button
                v-for="action in row.actions"
                :key="action"
                class="tc-btn"
                type="button"
                :data-im="'task-' + action"
                :data-intent-id="row.intentId"
                v-qio-tip="taskControlHint(action)"
                :disabled="Boolean(busy)"
                @click="runControl(row, action)"
              ><QIcon :name="taskActionIcon(action)" :size="11" aria-hidden="true" />{{ taskControlLabel(action, busy === row.intentId + ':' + action) }}</button>
              <button class="tc-btn ghost" type="button" data-im="task-record" @click="openRecord(row)"><QIcon name="clock" :size="11" aria-hidden="true" />执行记录</button>
            </span>
          </li>
        </ul>
      </section>

      <section v-if="waitingRows.length" class="tc-block" data-im="task-pending">
        <h3 class="tc-block-title">待确认 / 待处理（{{ waitingRows.length }}）</h3>
        <ul class="tc-list">
          <li
            v-for="row in waitingRows"
            :key="row.intentId"
            class="tc-row"
            data-im="task-pending-row"
            :data-intent-id="row.intentId"
            :data-status="row.status"
          >
            <label v-if="batchMode && !row.fromView" class="tc-check">
              <input
                type="checkbox"
                data-im="task-select"
                :checked="selected.includes(row.intentId)"
                @change="toggleSelected(row.intentId)"
              />
            </label>
            <span class="tc-name">{{ row.title }}</span>
            <span class="tc-status mono" :data-tone="row.tone" data-im="task-status">
              <QIcon :name="taskStatusIcon(row)" :size="12" aria-hidden="true" />
              {{ row.statusText }}
            </span>
            <span class="tc-actions" data-im="task-actions">
              <template v-if="!row.fromView">
                <button
                  class="tc-btn"
                  type="button"
                  :data-im="'task-approve'"
                  :data-intent-id="row.intentId"
                  :disabled="Boolean(busy)"
                  @click="decide(row, 'approve')"
                ><QIcon name="check" :size="11" aria-hidden="true" />{{ busy === row.intentId + ':approve' ? "处理中…" : "批准" }}</button>
                <button
                  class="tc-btn"
                  type="button"
                  :data-im="'task-reject'"
                  :data-intent-id="row.intentId"
                  :disabled="Boolean(busy)"
                  @click="decide(row, 'reject')"
                ><QIcon name="close" :size="11" aria-hidden="true" />{{ busy === row.intentId + ':reject' ? "处理中…" : "拒绝" }}</button>
              </template>
              <span v-else class="tc-hint">在板面的虚拟意图卡上确认（同批确认也在那里）</span>
            </span>
          </li>
        </ul>
        <div v-if="showBatch" class="tc-batch" data-im="task-batch">
          <span class="tc-hint" data-im="task-batch-hint">已选 {{ batchIds.length }} 条（同批至少 2 条）</span>
          <button
            class="tc-btn"
            type="button"
            data-im="task-batch-approve"
            :disabled="Boolean(busy) || batchIds.length < 2"
            @click="decideBatch('approve')"
          >同批批准</button>
          <button
            class="tc-btn"
            type="button"
            data-im="task-batch-reject"
            :disabled="Boolean(busy) || batchIds.length < 2"
            @click="decideBatch('reject')"
          >同批拒绝</button>
        </div>
      </section>

      <section v-if="finishedRows.length" class="tc-block" data-im="task-finished">
        <h3 class="tc-block-title">已结束（{{ finishedRows.length }}）</h3>
        <ul class="tc-list">
          <li
            v-for="row in finishedRows"
            :key="row.intentId"
            class="tc-row"
            data-im="task-row"
            :data-intent-id="row.intentId"
            :data-status="row.status"
            :data-execution="row.executionStatus"
          >
            <span class="tc-name">{{ row.title }}</span>
            <span class="tc-status mono" :data-tone="row.tone" data-im="task-status">
              <QIcon :name="taskStatusIcon(row)" :size="12" aria-hidden="true" />
              {{ row.statusText }}
            </span>
            <span class="tc-actions" data-im="task-actions">
              <button
                v-if="row.recoverable"
                class="tc-btn"
                type="button"
                :data-im="'task-recoverable'"
                :data-intent-id="row.intentId"
                @click="notice = '这条任务可以恢复：在输入区的「恢复与收尾」里处理（不会自动执行操作）。'"
              ><QIcon name="restore" :size="11" aria-hidden="true" />可恢复</button>
              <button class="tc-btn ghost" type="button" data-im="task-record" @click="openRecord(row)"><QIcon name="clock" :size="11" aria-hidden="true" />执行记录</button>
            </span>
          </li>
        </ul>
      </section>
    </template>
    </template>
  </section>
</template>

<style scoped>
.tc {
  position: absolute;
  top: 96px;
  right: 12px;
  z-index: calc(var(--im-z-dialog) + 10);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  width: min(480px, calc(100vw - 24px));
  max-height: min(60vh, 560px);
  overflow-y: auto;
  padding: var(--sp-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-2);
  color: var(--text-primary);
  font-family: var(--sans);
}
.tc-head { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
/* 分区标签：两个按钮就是全部导航，不做抽屉/浮层；窄窗换行也不横向溢出。 */
.tc-tabs { display: flex; gap: 4px; flex-wrap: wrap; border-bottom: 1px solid var(--border-subtle); padding-bottom: 2px; }
.tc-tab {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: none;
  border: 1px solid transparent;
  border-radius: var(--r-pill);
  padding: 2px var(--sp-2);
  cursor: pointer;
  min-width: 0;
}
.tc-tab:hover { color: var(--text-strong); border-color: var(--border-subtle); }
.tc-tab[data-active="1"] { color: var(--text-strong); border-color: var(--border-strong); background: var(--bg-inset); }
.tc-tab:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
/* 审批结果分区：只给滚动与宽度约束，面板自己负责内部滚动（窄窗 480 不横向溢出）。 */
.tc-results { min-width: 0; min-height: 0; overflow-x: hidden; }
.tc-title { margin: 0; flex: 1; min-width: 0; font-family: var(--serif); font-size: var(--fs-base); color: var(--text-strong); }
.tc-counts { flex: none; font-size: 10.5px; color: var(--text-muted); }
.tc-block { display: flex; flex-direction: column; gap: var(--sp-1); }
.tc-block-title { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); }
.tc-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.tc-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  padding: 4px var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
}
.tc-name { flex: 1; min-width: 0; font-size: var(--fs-xs); color: var(--text-primary); overflow-wrap: anywhere; }
/* #84：任务名是主信息（衬线、略大），状态是次信息（带共享图标的小胶囊），操作最轻（次级按钮）。 */
.tc-name { font-size: var(--fs-base); }
.tc-status {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 10.5px;
  color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  padding: 0 6px;
}
.tc-status .qio-icon { flex: none; }
.tc-status[data-tone="active"] .qio-icon { color: var(--accent); }
.tc-status[data-tone="active"] { color: var(--accent); border-color: var(--accent); }
.tc-status[data-tone="waiting"] { color: var(--warning); border-color: var(--warning); }
.tc-status[data-tone="problem"] { color: var(--danger); border-color: var(--danger); }
.tc-status[data-tone="settled"] { color: var(--success); border-color: var(--success); }
.tc-actions { display: flex; flex-wrap: wrap; gap: 4px; }
.tc-btn {
  font: inherit;
  font-size: 10.5px;
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  padding: 1px 8px;
  cursor: pointer;
}
.tc-btn { display: inline-flex; align-items: center; gap: 3px; }
.tc-btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
.tc-btn:disabled { color: var(--text-muted); cursor: default; opacity: 0.7; }
.tc-btn.ghost { border-color: transparent; }
.tc-link {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
}
.tc-warn, .tc-notice, .tc-empty, .tc-hint { margin: 0; font-size: var(--fs-xs); line-height: 1.6; overflow-wrap: anywhere; }
.tc-warn { color: var(--warning); }
/* #07：每一条独立读取失败是一块错误（可重试），不是一行混在一起的通知。 */
.tc-read-error { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px var(--sp-2); }
.tc-read-head { display: inline-flex; flex-wrap: wrap; align-items: baseline; gap: 4px; flex: 1 1 240px; min-width: 0; }
.tc-read-head strong { font-weight: 600; }
.tc-notice { color: var(--text-secondary); }
.tc-empty { color: var(--text-muted); }
.tc-hint { color: var(--text-muted); font-size: 10.5px; }
.tc-check { display: inline-flex; align-items: center; }
.tc-batch { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; padding-top: 2px; }
</style>
