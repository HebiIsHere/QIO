<script setup lang="ts">
/**
 * 审批弹窗（完整窗口）。
 *
 * 契约 §1.3：审批事实的整理**只有一处** —— 这里与过程区内联卡共用
 * `ApprovalFacts.vue`（同一份 approvalFacts）。弹窗自己只负责：
 * 出现/退出、焦点管理、队列门控与按钮层级；不再自己算一套显示规则。
 */
import { computed, nextTick, onMounted, ref, watch } from "vue";
import {
  useApprovalsStore,
  approvalFacts,
  confirmationProgressText,
  isLifecycleApproval,
} from "../stores/approvals";
import type { ApprovalItem, ApprovalBudget } from "../stores/approvals";
import { approvalStateLabel } from "../services/approvalLifecycle";
import { usePresence } from "../composables/usePresence";
import ApprovalFacts from "./ApprovalFacts.vue";
import { vQioScrollbar } from "../composables/useQioScrollbars";

const approvals = useApprovalsStore();
/** 退出动画期间还要渲染内容：保留一份「最后显示的审批」快照 */
const shownItem = ref<ApprovalItem | null>(null);
watch(
  () => approvals.current,
  (cur) => {
    if (cur) shownItem.value = cur;
  },
  { immediate: true },
);
const presence = usePresence(() => !!approvals.current && approvals.visible, 150);
const item = computed(
  () => (approvals.visible ? approvals.current : null) ?? (presence.mounted.value ? shownItem.value : null),
);
const dialogRef = ref<HTMLElement | null>(null);
/** 提交中的 approval_id（与 store.responding 同步；用于按钮文案/禁用） */
const submitting = computed(() => !!item.value && approvals.responding === item.value.approval_id);

const KIND_LABELS: Record<string, string> = {
  tool_create: "工具创建审批",
  credential_grant: "凭据授权审批",
  high_impact_knowledge: "高影响知识确认",
  tool_execution: "需要你确认的操作",
  computer: "电脑操作审批",
  // 为声明了第三方依赖的工具准备专用环境（安装是一次网络 + 磁盘动作）
  dependency_install: "安装依赖",
};
/** 标题一律说人话：后端出现新 kind 时也不要显示英文枚举名给用户 */
function kindTitle(kind: string): string {
  return KIND_LABELS[kind] ?? "需要你确认的操作";
}

/** 共用事实（与内联卡同一份整理） */
const facts = computed(() => approvalFacts(item.value));

/** R9 生命周期 v2：状态机 / 内部数据三次确认（前端只呈现，后端 CAS 为准）。 */
const lifecycle = computed(() => isLifecycleApproval(item.value));
const requiredConfirmations = computed(() =>
  Math.max(1, Number(item.value?.requiresConfirmations ?? 1)),
);
const doneConfirmations = computed(() =>
  Math.max(0, Math.min(requiredConfirmations.value, Number(item.value?.confirmedStage ?? 0))),
);
const needsConfirm = computed(() => requiredConfirmations.value > 1 && doneConfirmations.value < requiredConfirmations.value);
const confirmButtonText = computed(
  () =>
    "继续确认（第 " +
    (doneConfirmations.value + 1) +
    "/" +
    requiredConfirmations.value +
    " 次）",
);
/** 已批准 / 执行中：没有更多用户动作，只等执行结果（恢复出来的 pending 可能出现这两种状态）。 */
const awaitingExecution = computed(() => {
  const state = item.value?.state;
  return state === "approved" || state === "executing";
});
/** 内部数据警告文案（确认进度 + 边界；后端为准，前端不自行推进 stage）。 */
const confirmHint = computed(() => {
  if (!needsConfirm.value) return "";
  return (
    "这是 QIO 的内部运行数据，直接改写可能损坏应用：需要连续 " +
    requiredConfirmations.value +
    " 次确认。已确认 " +
    doneConfirmations.value +
    "/" +
    requiredConfirmations.value +
    " 次；" +
    confirmationProgressText(item.value)
  );
});

/** 「稍后处理」在已批准/执行中时没有待办意义：改为「知道了」收起这条状态。 */
function onLater() {
  if (awaitingExecution.value && item.value) {
    approvals.resolve(item.value.approval_id);
    return;
  }
  approvals.defer();
}

/** 打开弹窗时顺带刷新执行结果历史（只读补充事实；失败不打断审批本身）。 */
watch(
  () => [item.value?.approval_id ?? null, approvals.visible] as const,
  ([id, visible]) => {
    if (id && visible && !approvals.historyLoading) void approvals.loadHistory();
  },
  { immediate: true },
);

/**
 * 子 agent 预算（编辑后批准时随 overrides 提交）。
 *
 * 初值取这一条审批真实带的预算；编辑入口由 ApprovalFacts 渲染并回传。
 */
const budgetForm = ref<ApprovalBudget | null>(null);
watch(
  () => item.value?.approval_id,
  () => {
    budgetForm.value = facts.value?.budget ?? null;
  },
  { immediate: true },
);

function approveWithBudget() {
  const b = budgetForm.value ??
    facts.value?.budget ?? { maxIterations: 5, maxTokens: 100000, outputLimitChars: 2000 };
  approvals.respond("approved", {
    subagent_budget: {
      max_iterations: b.maxIterations,
      max_tokens: b.maxTokens,
      output_limit_chars: b.outputLimitChars,
    },
  });
}

/**
 * 高风险（会联网 / 写文件 / 执行命令 / 用凭据）：
 * 用于给批准按钮降调 —— 高风险操作不该看起来像「推荐你点它」。
 */
const highRisk = computed(() => facts.value?.highRisk === true);

/**
 * 提交失败时把焦点收回对话框：失败会保留待审批项，用户应当能立刻用键盘重试，
 * 而不是焦点掉到 body（实测：点「拒绝」失败后焦点标签是 BODY）。
 */
watch(
  () => approvals.error,
  async (err) => {
    if (!err) return;
    await nextTick();
    dialogRef.value?.focus();
  },
);

/**
 * 焦点管理：
 * - 审批出现时把焦点移进对话框（失败后重试也走这里）；
 * - 审批关闭时把焦点还给「被打断之前正在操作的元素」。
 *   后台审批可能叠在别的表单上，关掉之后焦点落到 body 会让用户
 *   既不知道回到哪里，也无法接着敲键盘。
 */
let restoreFocusTo: HTMLElement | null = null;
watch(
  // 注意：这里必须盯真实的队列项与「窗口是否可见」，而不是带退出快照的 item，
  // 否则「队列清空 / 稍后处理」这一步的变化会被快照抹平，焦点就还不回去。
  () => [approvals.current?.approval_id ?? null, approvals.visible] as const,
  async ([id], prev) => {
    // immediate 调用时没有旧值，必须给默认值
    const [prevId, prevVisible] = prev ?? [null, false];
    if (id && approvals.visible) {
      if (!prevId || !prevVisible) {
        const active = document.activeElement;
        // 如果焦点本来就在对话框里（稍后处理再打开），不要把它记成「被打断的元素」
        if (!dialogRef.value?.contains(active as Node)) {
          restoreFocusTo = active instanceof HTMLElement ? active : null;
        }
      }
      await nextTick();
      dialogRef.value?.focus();
      return;
    }
    const target = restoreFocusTo;
    restoreFocusTo = null;
    if (target && target.isConnected) {
      await nextTick();
      target.focus();
    }
  },
  { immediate: true },
);

onMounted(async () => {
  await nextTick();
  dialogRef.value?.focus();
});

/**
 * 键盘行为：Tab 在对话框内循环；Esc 不做决定，但按最上层处理——收起窗口并保留
 * 待审批项（同时亮出「有 N 项操作等待确认」入口），不是「看不见的待办」。
 */
function onKeydown(e: KeyboardEvent) {
  if (e.key === "Escape") {
    e.preventDefault();
    approvals.defer();
    return;
  }
  if (e.key !== "Tab") return;
  const root = dialogRef.value;
  if (!root) return;
  // summary 也是键盘可达元素（高级详情），必须参与 Tab 循环，否则焦点可能从它溜到对话框外
  const focusables = Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), summary, [href], [tabindex]:not([tabindex="-1"])',
    ),
  );
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const active = document.activeElement as HTMLElement | null;
  if (e.shiftKey && (active === first || active === root)) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && active === last) {
    e.preventDefault();
    first.focus();
  }
}
</script>

<template>
  <div v-if="presence.mounted.value && item" class="modal-mask" :class="{ leaving: presence.leaving.value }">
    <div
      ref="dialogRef"
      class="modal qio-card"
      :class="{ 'risk-high': highRisk }"
      :data-state="submitting ? 'running' : 'waiting'"
      role="dialog"
      aria-modal="true"
      :aria-labelledby="'approval-title'"
      tabindex="-1"
      @keydown="onKeydown"
    >
      <h3 id="approval-title" :title="item.kind">{{ kindTitle(item.kind) }}</h3>
      <div class="body" v-qio-scrollbar data-qio-fit>
        <!-- 共用事实：内联卡与本弹窗说同样的话（契约 §1.3） -->
        <ApprovalFacts :item="item" :budget="budgetForm" @update:budget="budgetForm = $event" />

        <!--
          R9：执行结果历史（GET /api/approvals/history；只读补充事实）。
          与审批事实分开展示，不把「历史结果」冒充成「这次会发生什么」。
        -->
        <details v-if="lifecycle" class="approval-history" data-test="approval-history">
          <summary>执行结果历史（只读）</summary>
          <p v-if="approvals.historyLoading" class="history-note">正在读取…</p>
          <p v-if="approvals.historyError" class="history-error" role="status">{{ approvals.historyError }}</p>
          <p v-if="approvals.lastResult" class="history-note" data-test="approval-last-result">
            最近一次结局：{{ approvalStateLabel(approvals.lastResult.state) }}
            <template v-if="approvals.lastResult.summary"> · {{ approvals.lastResult.summary }}</template>
          </p>
          <ul v-if="approvals.history.length" class="history-list" data-test="approval-history-list">
            <li v-for="row in approvals.history.slice(0, 8)" :key="row.approval_id + row.at">
              <span class="mono">{{ row.at }}</span>
              <span class="history-outcome">{{ approvalStateLabel(row.outcome) }}</span>
              <span class="history-summary">{{ row.summary }}</span>
            </li>
          </ul>
          <p v-else-if="!approvals.historyLoading" class="history-note">还没有执行结果历史。</p>
        </details>
      </div>
      <p v-if="approvals.error" class="approval-error" role="alert">{{ approvals.error }}</p>
      <div class="actions">
        <!-- 失效的确认（后端已无此审批）：只给一个出口，不再提供批准/拒绝 -->
        <button
          v-if="item.stale"
          class="later approval-stale-ack"
          type="button"
          @click="approvals.resolve(item.approval_id)"
        >
          知道了
        </button>
        <template v-else>
          <!-- 已批准 / 执行中（恢复出来的状态）：没有更多用户动作，只等执行结果 -->
          <p v-if="awaitingExecution" class="executing-note" data-test="approval-awaiting-execution">
            已批准，等待后端执行结果；不需要再操作。
          </p>
          <button
            v-if="!awaitingExecution"
            class="reject"
            :disabled="submitting"
            @click="approvals.respond('rejected')"
          >
            拒绝
          </button>
          <button
            class="later"
            :disabled="submitting"
            :title="awaitingExecution ? '收起这条状态' : '只收起窗口，保留待审批任务：既不批准也不拒绝'"
            @click="onLater"
          >
            {{ awaitingExecution ? "知道了" : "稍后处理" }}
          </button>
          <!-- 内部数据三次确认：按钮是「第 N 次确认」，不是一次批准；stage 由后端 CAS -->
          <button
            v-if="needsConfirm"
            class="approve"
            :disabled="submitting"
            :title="confirmHint"
            data-test="approval-confirm-next"
            @click="approvals.confirmNext()"
          >
            {{ submitting ? "提交中…" : confirmButtonText }}
          </button>
          <button
            v-else-if="facts?.isSubagentCreate"
            class="approve"
            :disabled="submitting"
            @click="approveWithBudget"
          >
            {{ submitting ? "提交中…" : "允许此次操作（含预算）" }}
          </button>
          <button
            v-else-if="!awaitingExecution"
            class="approve"
            :disabled="submitting"
            @click="approvals.respond('approved')"
          >
            {{ submitting ? "提交中…" : "允许此次操作" }}
          </button>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; z-index: 200; background: var(--bg-overlay);
  display: flex; align-items: center; justify-content: center;
  /* 出现：立即有可见变化随后减速；退出：短淡出后卸载。
     注意退出期间遮罩仍拦截点击（不让误点落到下层危险操作），
     但内容本身不可交互；退出结束整块从 DOM 移除，不留透明残留。 */
  animation: approval-mask-in var(--dur-menu) var(--ease-out) both;
  transition: opacity var(--dur-exit) var(--ease-in);
}
.modal-mask.leaving { opacity: 0; }
.modal-mask.leaving .modal { pointer-events: none; }
@keyframes approval-mask-in { from { opacity: 0; } to { opacity: 1; } }
.modal {
  width: 480px; max-width: 90vw; background: var(--bg-elevated); border: 1px solid var(--border-strong);
  border-radius: var(--r-lg); padding: 18px 20px; color: var(--text-primary);
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.45);
  animation: approval-modal-in var(--dur-menu) var(--ease-out) both;
  transition: transform var(--dur-exit) var(--ease-in), opacity var(--dur-exit) var(--ease-in);
}
.modal-mask.leaving .modal { transform: translateY(2px) scale(.995); opacity: 0; }
@keyframes approval-modal-in { from { opacity: 0; transform: translateY(var(--shift-8)) scale(.99); } to { opacity: 1; transform: none; } }
.modal:focus { outline: none; }
.modal:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.modal h3 { margin: 0 0 10px; font-size: 16px; color: var(--text-strong); }
/* 内容变多（事实与高级详情）后，320px 会把「它想做什么」挤出首屏——
   审批弹窗的第一句必须一眼可见，所以按视口给高度，仍然允许内部滚动。
   滚动条走统一策略：原生外观已全局移除，这里用共享 QIO 指示条；
   data-qio-fit + 软换行保证事实区不横向溢出。
   按钮区（.actions）留在 .body **外面**：正文滚动与固定操作分离，任何长度的事实都藏不掉按钮。 */
.body {
  max-height: min(58vh, 420px);
  overflow-y: auto;
  overscroll-behavior: contain;
  min-width: 0;
}
.actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; }
/* 按钮尺寸对齐统一按钮原语（.qio-btn）：高度 34、圆角 r-md、内边距 18。
    以前这里是 18px 圆角 + 26px 内边距，看起来像另一个产品里的按钮。 */
.actions button {
  border: none; border-radius: var(--r-md); padding: 0 18px; height: 34px;
  cursor: pointer; font-size: 13px; font-family: var(--sans);
  min-width: 96px; /* 处理中改文案也不会让按钮尺寸跳动 */
  transition: transform var(--dur-press) var(--ease-1-out), filter var(--dur-fast) var(--ease-1);
}
.actions button:active:not(:disabled) { transform: translateY(var(--shift-1)); }
.actions button:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
/* 「稍后处理」：只收起窗口、保留待审批任务。外观比拒绝更轻，避免看起来像第三种决定 */
.later {
  background: transparent; color: var(--text-secondary); border: 1px dashed var(--border-strong);
}
.later:hover:not(:disabled) { color: var(--text-primary); border-color: var(--text-muted); }
/* 按钮层级（任务 04 PART C3）：
   批准＝primary（品牌强调色），拒绝＝secondary（中性描边）。
   高风险时批准按钮降调为中性实心：它是「确认执行」，不是品牌在推荐你点。 */
.approve { background: var(--accent); color: var(--on-accent); border: 1px solid transparent; }
.approve:hover:not(:disabled) { background: var(--accent-hover); }
.risk-high .approve {
  background: var(--bg-inset); color: var(--text-strong); border-color: var(--border-strong);
}
.risk-high .approve:hover:not(:disabled) { background: var(--bg-surface); }
/* 选择器带 .actions 以提高优先级：上面 `.actions button { border: none }` 会覆盖裸 .reject */
.actions .reject {
  background: transparent; color: var(--text-primary); border: 1px solid var(--border-strong);
}
.actions .reject:hover:not(:disabled) { background: var(--bg-inset); }
.actions button:disabled { opacity: 0.5; }
.actions button:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.approval-history {
  margin-top: 12px;
  border-top: 1px solid var(--border-subtle);
  padding-top: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}
.approval-history > summary {
  cursor: pointer;
  color: var(--text-secondary);
  list-style: none;
}
.approval-history > summary::-webkit-details-marker { display: none; }
.approval-history > summary::before { content: "›"; display: inline-block; margin-right: 4px; color: var(--text-muted); }
.approval-history[open] > summary::before { transform: rotate(90deg); }
.history-note { margin: 6px 0 0; color: var(--text-muted); }
.history-error { margin: 6px 0 0; color: var(--danger); }
.history-list { margin: 6px 0 0; padding-left: 18px; }
.history-list li { margin: 2px 0; overflow-wrap: anywhere; }
.history-list .mono { color: var(--text-muted); font-size: 11px; margin-right: 6px; }
.history-outcome { color: var(--text-strong); margin-right: 6px; }
.executing-note { margin: 0 auto 0 0; color: var(--text-secondary); font-size: 12.5px; align-self: center; }
.approval-error {
  margin-top: 12px;
  padding: 8px 12px;
  border: 1px solid var(--danger);
  border-radius: 8px;
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 12.5px;
  line-height: 1.5;
}
</style>
