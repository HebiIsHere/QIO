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
import ApprovalResultsPanel from "../../approval/ApprovalResultsPanel.vue";
import {
  batchDecideIds,
  taskControlHint,
  taskControlLabel,
  taskEmptyText,
  type TaskControlAction,
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
const servicePending = computed(() => waitingRows.value.filter((row) => row.fromView));
const batchIds = computed(() => batchDecideIds(rows.value, selected.value));
const counts = computed(() => store.taskCounts);

const emptyText = computed(() => {
  if (props.focus === "pending" && legacyPending.value.length === 0 && servicePending.value.length === 0) {
    return taskEmptyText("waiting");
  }
  return taskEmptyText();
});

function toggleSelected(intentId: string): void {
  selected.value = selected.value.includes(intentId)
    ? selected.value.filter((id) => id !== intentId)
    : [...selected.value, intentId];
}

async function refresh(): Promise<void> {
  await store.refreshAfterIntentAction();
}

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
      <span class="tc-counts mono" data-im="task-counts">
        执行中 {{ counts.live }} · 已暂停 {{ counts.paused }} · 待处理 {{ counts.waiting }} · 已结束 {{ counts.finished }}
      </span>
      <button class="tc-btn" type="button" data-im="task-refresh" @click="refresh">刷新</button>
      <button class="tc-btn" type="button" data-im="task-close" aria-label="关闭任务面板" @click="emit('close')">×</button>
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
    <p v-if="store.intentViewsLoadState === 'error'" class="tc-warn" role="alert" data-im="task-read-error">
      任务清单更新失败：{{ store.intentViewsLoadError || "原因未知" }}（当前显示上次读到的任务，不代表没有任务）
      <button class="tc-link" type="button" data-im="task-read-retry" @click="refresh">重试读取</button>
    </p>

    <p v-if="notice" class="tc-notice" role="status" data-im="task-notice">{{ notice }}</p>

    <p v-if="!rows.length" class="tc-empty" data-im="task-empty">{{ emptyText }}</p>

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
            <span class="tc-status mono" :data-tone="row.tone">{{ row.statusText }}</span>
            <span class="tc-actions">
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
              >{{ taskControlLabel(action, busy === row.intentId + ':' + action) }}</button>
              <button class="tc-btn ghost" type="button" data-im="task-record" @click="openRecord(row)">执行记录</button>
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
            <span class="tc-status mono" :data-tone="row.tone">{{ row.statusText }}</span>
            <span class="tc-actions">
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
              >{{ taskControlLabel(action, busy === row.intentId + ':' + action) }}</button>
              <button class="tc-btn ghost" type="button" data-im="task-record" @click="openRecord(row)">执行记录</button>
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
            <label v-if="!row.fromView" class="tc-check">
              <input
                type="checkbox"
                :checked="selected.includes(row.intentId)"
                @change="toggleSelected(row.intentId)"
              />
            </label>
            <span class="tc-name">{{ row.title }}</span>
            <span class="tc-status mono" :data-tone="row.tone">{{ row.statusText }}</span>
            <span class="tc-actions">
              <template v-if="!row.fromView">
                <button
                  class="tc-btn"
                  type="button"
                  :data-im="'task-approve'"
                  :data-intent-id="row.intentId"
                  :disabled="Boolean(busy)"
                  @click="decide(row, 'approve')"
                >{{ busy === row.intentId + ':approve' ? "处理中…" : "批准" }}</button>
                <button
                  class="tc-btn"
                  type="button"
                  :data-im="'task-reject'"
                  :data-intent-id="row.intentId"
                  :disabled="Boolean(busy)"
                  @click="decide(row, 'reject')"
                >{{ busy === row.intentId + ':reject' ? "处理中…" : "拒绝" }}</button>
              </template>
              <span v-else class="tc-hint">在板面的虚拟意图卡上确认（同批确认也在那里）</span>
            </span>
          </li>
        </ul>
        <div v-if="legacyPending.length >= 2" class="tc-batch">
          <span class="tc-hint">已选 {{ batchIds.length }} 条（同批至少 2 条）</span>
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
            <span class="tc-status mono" :data-tone="row.tone">{{ row.statusText }}</span>
            <span class="tc-actions">
              <button
                v-if="row.recoverable"
                class="tc-btn"
                type="button"
                :data-im="'task-recoverable'"
                :data-intent-id="row.intentId"
                @click="notice = '这条任务可以恢复：在输入区的「恢复与收尾」里处理（不会自动执行操作）。'"
              >可恢复</button>
              <button class="tc-btn ghost" type="button" data-im="task-record" @click="openRecord(row)">执行记录</button>
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
  border-radius: var(--qio-ui-radius);
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
.tc-status { flex: none; font-size: 10.5px; color: var(--text-secondary); border: 1px solid var(--border-subtle); border-radius: var(--qio-ui-radius); padding: 0 6px; }
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
  border-radius: var(--qio-ui-radius);
  padding: 1px 8px;
  cursor: pointer;
}
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
.tc-notice { color: var(--text-secondary); }
.tc-empty { color: var(--text-muted); }
.tc-hint { color: var(--text-muted); font-size: 10.5px; }
.tc-check { display: inline-flex; align-items: center; }
.tc-batch { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; padding-top: 2px; }
</style>
