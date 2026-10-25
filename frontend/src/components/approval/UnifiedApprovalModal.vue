<script setup lang="ts">
/**
 * 统一审批弹窗（D）：所有需要明确批准/拒绝的单次申请都在这里处理。
 *
 * - 固定位置、一次只展示一项；多项时可在队列里主动切换（新申请不替换正在看的）；
 * - 类型分派在 store 里按结构化身份完成：工具/权限 → /api/approvals/{id}/respond，
 *   知识保存 → /api/knowledge/{id}/approval（修改后保存提交的是用户编辑后的内容）；
 * - 关闭/稍后只隐藏，不批准也不拒绝；stale（后端已无此审批）只给「知道了」；
 * - 系统事实与模型说明的分工完全复用 ApprovalFacts（不另写一套显示规则）。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import {
  useApprovalsStore,
  approvalFacts,
  isLifecycleApproval,
  type ApprovalBudget,
} from "../../stores/approvals";
import { approvalStateLabel } from "../../services/approvalLifecycle";
import {
  TOOL_ORIGIN_LABELS,
  toolSourceFacts,
  unifiedItemGroupLabel,
  unifiedItemSummary,
  useUnifiedApprovalsStore,
} from "../../stores/unifiedApprovals";
import ApprovalFacts from "../ApprovalFacts.vue";
import ApprovalResultsPanel from "./ApprovalResultsPanel.vue";
import KnowledgeApprovalBody from "./KnowledgeApprovalBody.vue";
import { vQioScrollbar } from "../../composables/useQioScrollbars";

const unified = useUnifiedApprovalsStore();
const approvals = useApprovalsStore();

const item = computed(() => (unified.visible ? unified.activeItem : null));
/** 弹窗打开条件：有待审批（队列视图）或用户明确打开了「结果与历史」（F02：0 待办也可见）。 */
const panelOpen = computed(() => Boolean(item.value) || unified.resultsVisible);
const items = computed(() => unified.items);
const activeIndex = computed(() => unified.activeIndex);
const busy = computed(() => !!item.value && unified.submittingKey === item.value.key);
const dialogRef = ref<HTMLElement | null>(null);

const toolFacts = computed(() =>
  item.value?.approval ? approvalFacts(item.value.approval) : null,
);
const budgetForm = ref<ApprovalBudget | null>(null);
/**
 * 用户是否真的改过预算（F06）。
 *
 * 没改就不发 overrides —— 否则「默认值」会静默覆盖提案里原本更小/更大的预算，
 * 表现为「没改也生效了修改」。只有 dirty 才把用户批准的值传给后端。
 */
const budgetDirty = ref(false);
const extraConfirm = ref(false);

/** 重点确认（会删除/覆盖）需要额外一次勾选，降低误点。 */
const needExtraConfirm = computed(() => toolFacts.value?.sideEffect === "destructive");
const canApprove = computed(
  () => !busy.value && (!needExtraConfirm.value || extraConfirm.value),
);

const sourceFacts = computed(() =>
  item.value && item.value.kind === "tool" && item.value.approval
    ? toolSourceFacts({
        kind: item.value.approval.kind,
        origin: item.value.origin,
        approvalId: item.value.approval.approval_id,
        payload: item.value.approval.payload,
      })
    : [],
);

const kicker = computed(() => {
  if (!item.value) return "";
  if (item.value.kind === "knowledge") {
    return "知识保存申请 · 确认前尚未生效";
  }
  const origin = TOOL_ORIGIN_LABELS[item.value.origin] ?? "来源未记录";
  return origin + "提出的申请 · " + (item.value.highRisk ? "重点确认" : "单次授权");
});

const errorText = computed(() => {
  if (!item.value) return "";
  if (unified.submitError) return unified.submitError;
  if (item.value.kind === "tool" && item.value.approval) return approvals.error ?? "";
  return "";
});

/**
 * R9 生命周期 v2（契约 §5）：状态机、确认阶段、期限全部来自后端
 * （store 由 APPROVAL_REQUIRED / APPROVAL_RESULT 事件、pending 恢复快照与接口返回值写入）。
 * 这里只做**呈现**：绝不自行推进状态、绝不本地把 confirmed_stage 加一。
 */
const lifecycle = computed(() => isLifecycleApproval(item.value?.approval ?? null));
const requiredConfirmations = computed(() =>
  Math.max(1, Number(item.value?.approval?.requiresConfirmations ?? 1)),
);
const doneConfirmations = computed(() =>
  Math.max(
    0,
    Math.min(requiredConfirmations.value, Number(item.value?.approval?.confirmedStage ?? 0)),
  ),
);
/** 内部运行数据：连续 N 次确认，按钮是「第 N/M 次」，不是一次批准。 */
const needsConfirm = computed(
  () =>
    requiredConfirmations.value > 1 && doneConfirmations.value < requiredConfirmations.value,
);
const confirmButtonText = computed(
  () =>
    "继续确认（第 " + (doneConfirmations.value + 1) + "/" + requiredConfirmations.value + " 次）",
);
/** 已批准 / 执行中（含重启恢复出来的状态）：没有更多用户动作，只等执行结果。 */
const awaitingExecution = computed(() => {
  const state = item.value?.approval?.state;
  return state === "approved" || state === "executing";
});
/** 状态标签（未知状态如实显示原值，不假装认识）。 */
const lifecycleStateLabel = computed(() =>
  lifecycle.value ? approvalStateLabel(String(item.value?.approval?.state ?? "")) : "",
);
const expiresText = computed(() => String(item.value?.approval?.expiresAt ?? "").trim());
/**
 * 失败原因按「可分辨的三类」加上前缀：已过期 / 已被处理 / 对象已变化。
 * 原文一律保留（后端中文原因本身就是最有用的信息），不重写成笼统的「失败了」。
 */
const failureHint = computed(() => {
  const raw = errorText.value;
  if (!raw) return "";
  if (raw.includes("有效期") || raw.includes("过期")) return "已过期 · " + raw;
  if (raw.includes("已经有结局") || raw.includes("已过期）") || raw.includes("不等待你的授权")) {
    return "已被处理 · " + raw;
  }
  if (raw.includes("更新过") || raw.includes("确认阶段") || raw.includes("revision")) {
    return "对象已变化 · " + raw;
  }
  return raw;
});

/** 已批准 / 执行中这一条不再等用户：把它从待办里收口（只收口，不伪造决定）。 */
function acknowledgeExecution() {
  const active = item.value;
  if (!active || active.kind !== "tool") return;
  approvals.resolve(active.identity);
  unified.noteQueueChanged();
}

const editing = computed(() => !!item.value && unified.editingKey === item.value.key);
const draft = computed(() => (item.value ? unified.draftFor(item.value.key) : ""));

watch(
  () => item.value?.key ?? null,
  async () => {
    extraConfirm.value = false;
    unified.stopEdit();
    budgetForm.value = toolFacts.value?.budget ?? null;
    budgetDirty.value = false;
    await nextTick();
  },
);

watch(
  () => [item.value?.key ?? null, unified.visible, unified.resultsVisible] as const,
  async ([, visible, resultsVisible]) => {
    if (!visible && !resultsVisible) return;
    await nextTick();
    dialogRef.value?.focus();
  },
  { immediate: true },
);

/** 关闭时把焦点还给被打断的元素（后台审批可能叠在别的输入上）。 */
let restoreFocusTo: HTMLElement | null = null;
watch(
  () => [unified.visible, item.value?.key ?? null] as const,
  ([visible, key], previous) => {
    const wasVisible = previous?.[0] ?? false;
    if (visible && !wasVisible) {
      const active = document.activeElement;
      if (!(active instanceof HTMLElement) || !dialogRef.value?.contains(active)) {
        restoreFocusTo = active instanceof HTMLElement ? active : null;
      }
    }
    if (!visible && wasVisible) {
      const target = restoreFocusTo;
      restoreFocusTo = null;
      if (target && target.isConnected) void nextTick().then(() => target.focus());
    }
    void key;
  },
);

onBeforeUnmount(() => {
  const target = restoreFocusTo;
  restoreFocusTo = null;
  if (target && target.isConnected) target.focus();
});

function onKeydown(event: KeyboardEvent) {
  if (event.key === "Escape") {
    event.preventDefault();
    // Esc = 关闭/稍后，不做出任何决定（结果视图同样只关闭）
    unified.closePanel();
    return;
  }
  if (event.key !== "Tab") return;
  const root = dialogRef.value;
  if (!root) return;
  const focusables = Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), textarea:not([disabled]), input:not([disabled]), summary, [href], [tabindex]:not([tabindex="-1"])',
    ),
  );
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const active = document.activeElement as HTMLElement | null;
  if (event.shiftKey && (active === first || active === root)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

/** 打开结果与历史视图（待审批为 0 时也能打开；同一弹窗内切换，不是第二套入口）。 */
function showResults() {
  unified.openResults();
}

/** 回到待审批队列（没有待办时保持结果视图）。 */
function showQueue() {
  unified.showQueue();
}

function goStep(delta: number) {
  const list = items.value;
  const next = list[activeIndex.value + delta];
  if (!next) return;
  unified.switchTo(next.key);
}

/**
 * 队列标签的摘要（F05）：同类型多条申请要能互相区分。
 * 用真实事实（工具用系统 description，知识用类别+内容摘要），不用类型枚举重复。
 */
function shortLabel(index: number): string {
  return unifiedItemSummary(items.value[index] ?? null);
}

/** 标签上的类型分组（权限 / 知识）。 */
function groupLabel(index: number): string {
  return unifiedItemGroupLabel(items.value[index] ?? null);
}

/** 标签的完整读屏文案（视觉截断不丢信息）。 */
function chipAria(index: number): string {
  const target = items.value[index];
  if (!target) return "";
  return groupLabel(index) + "：" + shortLabel(index);
}

async function submit(decision: "approve" | "approve_modified" | "ignore" | "reject") {
  const active = item.value;
  if (!active) return;
  if (active.kind === "tool") {
    const facts = toolFacts.value;
    /**
     * F06：只有「旧 tool_create 子智能体路径」+「用户真的改过预算」才发 overrides。
     * v2 生命周期记录（/decide 不支持 overrides）永远不发，避免假修改。
     */
    const overrides =
      decision === "approve" && facts?.isSubagentCreate && !lifecycle.value && budgetDirty.value
        ? {
            subagent_budget: {
              max_iterations: budgetForm.value?.maxIterations ?? 5,
              max_tokens: budgetForm.value?.maxTokens ?? 100000,
              output_limit_chars: budgetForm.value?.outputLimitChars ?? 2000,
            },
          }
        : undefined;
    await unified.submit(decision, { overrides });
    return;
  }
  await unified.submit(decision);
}

/** 用户改了预算：只有这次真的改动才允许随批准提交（F06）。 */
function onBudgetChange(next: ApprovalBudget) {
  budgetForm.value = next;
  budgetDirty.value = true;
}

function retryKnowledgeDetail() {
  void unified.refreshKnowledgePending();
}
</script>

<template>
  <div
    v-if="panelOpen"
    class="ua-mask"
    data-unified-approval-modal
    @click.self="unified.close()"
  >
    <section
      ref="dialogRef"
      class="ua-modal qio-card"
      data-unified-approval-modal
      role="dialog"
      aria-modal="true"
      aria-labelledby="ua-title"
      tabindex="-1"
      @keydown="onKeydown"
    >
      <header class="ua-head">
        <p class="ua-kicker mono">{{ unified.resultsVisible ? "审批结果与历史 · 只读记录" : kicker }}</p>
        <button
          class="ua-close"
          type="button"
          aria-label="关闭（不会自动批准或拒绝）"
          :title="unified.resultsVisible ? '关闭结果与历史（只读记录不会改变）' : '稍后处理：只收起窗口，保留申请'"
          @click="unified.closePanel()"
        >
          ×
        </button>
        <h2 id="ua-title" class="ua-title serif">
          {{ unified.resultsVisible ? "审批结果与历史" : item?.title ?? "" }}
        </h2>
      </header>

      <!-- 两个视图共用一个弹窗：待审批队列 / 结果与历史（F02；0 待办也能打开结果） -->
      <div class="ua-tabs" role="tablist" aria-label="审批视图">
        <button
          class="ua-tab"
          type="button"
          role="tab"
          :aria-selected="!unified.resultsVisible"
          :class="{ active: !unified.resultsVisible }"
          :disabled="!items.length"
          :title="items.length ? '查看等待你处理的申请' : '当前没有待审批的申请'"
          data-test="unified-approval-tab-queue"
          @click="showQueue"
        >
          待审批 {{ items.length }}
        </button>
        <button
          class="ua-tab"
          type="button"
          role="tab"
          :aria-selected="unified.resultsVisible"
          :class="{ active: unified.resultsVisible }"
          title="查看审批最终结果与后端历史（只读）"
          data-test="unified-approval-tab-results"
          @click="showResults"
        >
          结果与历史
        </button>
      </div>

      <template v-if="!unified.resultsVisible && item">

      <div v-if="items.length > 1" class="ua-switch" data-test="unified-approval-switch">
        <div class="ua-switch-line">
          <span class="ua-count mono">待审批 {{ items.length }} 项 · 当前 {{ activeIndex + 1 }}/{{ items.length }}</span>
          <div class="ua-nav">
            <button
              class="ua-nav-btn"
              type="button"
              aria-label="上一项"
              :disabled="activeIndex <= 0"
              @click="goStep(-1)"
            >
              ‹
            </button>
            <button
              class="ua-nav-btn"
              type="button"
              aria-label="下一项"
              :disabled="activeIndex >= items.length - 1"
              @click="goStep(1)"
            >
              ›
            </button>
          </div>
        </div>
        <div class="ua-queue" role="tablist" aria-label="待审批申请">
          <button
            v-for="(queued, index) in items"
            :key="queued.key"
            class="ua-chip"
            type="button"
            role="tab"
            :aria-selected="queued.key === item.key"
            :class="{ active: queued.key === item.key, deferred: queued.deferred }"
            :title="chipAria(index)"
            :aria-label="chipAria(index)"
            :data-test="'unified-approval-chip'"
            @click="unified.switchTo(queued.key)"
          >
            <span class="ua-chip-kind mono">{{ groupLabel(index) }}</span>
            <span class="ua-chip-text">{{ shortLabel(index) }}</span>
            <span v-if="queued.deferred" class="ua-chip-dot mono" aria-label="稍后处理过">·</span>
          </button>
        </div>
      </div>

      <div class="ua-body" v-qio-scrollbar>
        <template v-if="item.kind === 'tool' && item.approval">
          <div v-if="sourceFacts.length" class="ua-source" data-test="unified-approval-source">
            <span v-for="fact in sourceFacts" :key="fact.label" class="ua-source-fact">
              <span class="ua-source-label">{{ fact.label }}</span>
              <span class="ua-source-value" :class="{ mono: fact.label === '来源任务' || fact.label === '知识' }">{{ fact.value }}</span>
            </span>
          </div>
          <p v-if="item.stale" class="ua-stale" role="status">
            这条审批已经有结局（可能已在别处处理或已过期），不会再等待你的授权。未做出任何授权。
          </p>
          <ApprovalFacts
            :item="item.approval"
            :budget="budgetForm"
            :hide-lifecycle-status="true"
            @update:budget="onBudgetChange"
          />
        </template>
        <template v-else-if="item.knowledge">
          <KnowledgeApprovalBody
            :item="item.knowledge"
            :editing="editing"
            :draft="draft"
            :busy="busy"
            @update:draft="unified.updateDraft($event)"
            @edit="unified.startEdit()"
            @cancel-edit="unified.stopEdit()"
            @retry="retryKnowledgeDetail"
          />
        </template>
        <p v-if="unified.knowledgeError && item.kind === 'knowledge'" class="ua-refresh-note">
          {{ unified.knowledgeError }}
          <button class="ua-refresh-btn" type="button" @click="retryKnowledgeDetail">重试</button>
        </p>
      </div>

      <!-- R9：状态机与期限（后端事实；未带 state 的旧审批不渲染这一行） -->
      <p v-if="lifecycle && (lifecycleStateLabel || expiresText)" class="ua-lifecycle mono" data-test="unified-approval-lifecycle">
        <span v-if="lifecycleStateLabel" data-test="unified-approval-state">状态：{{ lifecycleStateLabel }}</span>
        <span v-if="expiresText" data-test="unified-approval-expires"> · 期限：{{ expiresText }}</span>
        <span v-if="requiredConfirmations > 1" data-test="unified-approval-progress">
          · 确认进度：{{ doneConfirmations }}/{{ requiredConfirmations }}
        </span>
      </p>

      <p v-if="errorText" class="ua-error" role="alert">{{ failureHint }}</p>

      <footer class="ua-actions">
        <template v-if="item.kind === 'tool'">
          <button
            v-if="item.stale"
            class="ua-btn later"
            type="button"
            data-test="unified-approval-stale-ack"
            @click="unified.dismissStale()"
          >
            知道了
          </button>
          <!-- 已批准 / 执行中（含重启恢复出来的状态）：只等后端结果，不再给决定按钮 -->
          <template v-else-if="awaitingExecution">
            <p class="ua-awaiting" role="status" data-test="unified-approval-awaiting">
              已批准，等待执行；后端执行完这里会显示结果，不需要再操作。
            </p>
            <button
              class="ua-btn later"
              type="button"
              data-test="unified-approval-awaiting-ack"
              @click="acknowledgeExecution()"
            >
              知道了
            </button>
          </template>
          <template v-else>
            <label v-if="needExtraConfirm" class="ua-confirm">
              <input v-model="extraConfirm" type="checkbox" />
              我确认要执行这次操作
            </label>
            <button
              class="ua-btn quiet"
              type="button"
              :disabled="busy"
              data-test="unified-approval-reject"
              @click="submit('reject')"
            >
              拒绝
            </button>
            <button class="ua-btn later" type="button" :disabled="busy" @click="unified.deferActive()">
              稍后处理
            </button>
            <!--
              内部运行数据：这是「第 N 次确认」，不是一次批准。
              stage 由后端 CAS 判定（前端只发 confirmed_stage+1，并用返回值覆盖进度）。
            -->
            <button
              v-if="needsConfirm"
              class="ua-btn primary"
              type="button"
              :disabled="busy"
              data-test="unified-approval-confirm-next"
              :title="'确认进度以后端为准：已确认 ' + doneConfirmations + '/' + requiredConfirmations + ' 次'"
              @click="submit('approve')"
            >
              {{ busy ? "提交中…" : confirmButtonText }}
            </button>
            <button
              v-else
              class="ua-btn primary"
              type="button"
              :disabled="!canApprove"
              data-test="unified-approval-allow"
              @click="submit('approve')"
            >
              {{ busy ? "提交中…" : "允许此次操作" }}
            </button>
          </template>
        </template>
        <template v-else>
          <template v-if="editing">
            <button class="ua-btn later" type="button" :disabled="busy" @click="unified.stopEdit()">
              取消修改
            </button>
            <button
              class="ua-btn primary"
              type="button"
              :disabled="busy"
              data-test="unified-approval-save-modified"
              @click="submit('approve_modified')"
            >
              {{ busy ? "保存中…" : "按修改后的内容保存" }}
            </button>
          </template>
          <template v-else>
            <button class="ua-btn later" type="button" :disabled="busy" @click="unified.deferActive()">
              稍后处理
            </button>
            <button
              class="ua-btn quiet"
              type="button"
              :disabled="busy"
              data-test="unified-approval-ignore"
              @click="submit('ignore')"
            >
              不保存
            </button>
            <button
              class="ua-btn quiet"
              type="button"
              :disabled="busy"
              data-test="unified-approval-edit"
              @click="unified.startEdit()"
            >
              修改
            </button>
            <button
              class="ua-btn primary"
              type="button"
              :disabled="busy"
              data-test="unified-approval-save"
              @click="submit('approve')"
            >
              {{ busy ? "提交中…" : "确认保存" }}
            </button>
          </template>
        </template>
      </footer>
      <p class="ua-note">每次只处理当前申请。关闭窗口不会自动批准或拒绝，也不会丢失申请。</p>
      </template>

      <!-- 结果与历史：只读；0 待办时也能打开（F02） -->
      <template v-else>
        <div class="ua-body ua-results-body" v-qio-scrollbar>
          <ApprovalResultsPanel />
        </div>
        <footer class="ua-actions">
          <button
            v-if="items.length"
            class="ua-btn quiet"
            type="button"
            data-test="unified-approval-back-queue"
            @click="showQueue"
          >
            回到待审批（{{ items.length }}）
          </button>
          <button
            class="ua-btn later"
            type="button"
            data-test="unified-approval-results-close"
            @click="unified.closePanel()"
          >
            关闭
          </button>
        </footer>
      </template>
    </section>
  </div>
</template>

<style scoped>
.ua-mask {
  position: fixed;
  inset: 0;
  z-index: 200;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--bg-overlay);
  backdrop-filter: blur(var(--glass-blur, 6px));
  animation: ua-mask-in var(--dur-menu) var(--ease-out) both;
}
@keyframes ua-mask-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
.ua-modal {
  width: min(540px, 100%);
  max-height: min(86vh, 720px);
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 18px 20px 14px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  background: var(--bg-elevated);
  color: var(--text-primary);
  box-shadow: var(--shadow-3);
  animation: ua-modal-in var(--dur-menu) var(--ease-out) both;
}
@keyframes ua-modal-in {
  from { opacity: 0; transform: translateY(var(--shift-8, 8px)) scale(0.99); }
  to { opacity: 1; transform: none; }
}
.ua-modal:focus {
  outline: none;
}
.ua-modal:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.ua-head {
  position: relative;
  padding-right: 28px;
}
.ua-kicker {
  margin: 0 0 6px;
  font-size: 10px;
  letter-spacing: 0.08em;
  color: var(--text-muted);
  text-transform: uppercase;
}
.ua-title {
  margin: 0;
  font-family: var(--serif);
  font-size: 19px;
  font-weight: 500;
  color: var(--text-strong);
  overflow-wrap: anywhere;
}
.ua-close {
  position: absolute;
  top: -2px;
  right: -2px;
  width: 28px;
  height: 28px;
  border: 1px solid transparent;
  border-radius: 50%;
  background: transparent;
  color: var(--text-secondary);
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
}
.ua-close:hover {
  background: var(--bg-inset);
  color: var(--text-strong);
}
.ua-close:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
/* 两个视图共用一个弹窗；标签是视图切换，不是第二套入口 */
.ua-tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--border-subtle);
}
.ua-tab {
  appearance: none;
  padding: 6px 10px;
  border: none;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--text-muted);
  font-family: var(--sans);
  font-size: 12px;
  cursor: pointer;
}
.ua-tab.active {
  color: var(--text-strong);
  border-bottom-color: var(--accent);
}
.ua-tab:disabled {
  opacity: 0.5;
  cursor: default;
}
.ua-tab:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.ua-results-body {
  padding-top: 4px;
}
.ua-switch {
  border-top: 1px solid var(--border-subtle);
  border-bottom: 1px solid var(--border-subtle);
  padding: 7px 0;
}
.ua-switch-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.ua-count {
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
.ua-nav {
  display: inline-flex;
  gap: 4px;
}
.ua-nav-btn {
  width: 24px;
  height: 22px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
}
.ua-nav-btn:disabled {
  opacity: 0.35;
  cursor: default;
}
.ua-nav-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
/* F05：480px 窄窗下 ≥3 条待审批标签必须**换行**显示（不是只藏滚动条），
   每条都带自己的操作摘要，不再是多个相同的「权限」标签。 */
.ua-queue {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 7px;
  padding-bottom: 2px;
}
.ua-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: 0 1 auto;
  min-width: 0;
  max-width: 100%;
  padding: 3px 9px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  background: transparent;
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: 11.5px;
  cursor: pointer;
}
.ua-chip.active {
  border-color: var(--accent);
  color: var(--text-strong);
  background: var(--accent-softer);
}
.ua-chip:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.ua-chip-kind {
  font-size: 9.5px;
  letter-spacing: 0.06em;
  color: var(--text-muted);
}
.ua-chip-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ua-chip-dot {
  color: var(--text-muted);
}
.ua-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding-right: 2px;
}
.ua-source {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin: 0 0 10px;
  padding: 8px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-inset);
  font-size: 11.5px;
}
.ua-source-fact {
  display: inline-flex;
  gap: 6px;
  min-width: 0;
}
.ua-source-label {
  color: var(--text-muted);
}
.ua-source-value {
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.ua-stale {
  margin: 0 0 10px;
  padding: 8px 10px;
  border: 1px solid var(--warning);
  border-radius: var(--r-md);
  background: var(--warning-soft, var(--bg-inset));
  color: var(--text-primary);
  font-size: 12px;
  line-height: 1.6;
}
.ua-refresh-note {
  margin: 8px 0 0;
  color: var(--text-muted);
  font-size: 11.5px;
}
.ua-refresh-btn {
  margin-left: 8px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--text-secondary);
  font-size: 11px;
  padding: 2px 8px;
  cursor: pointer;
}
.ua-error {
  margin: 0;
  padding: 8px 12px;
  border: 1px solid var(--danger);
  border-radius: var(--r-md);
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 12px;
  line-height: 1.55;
  overflow-wrap: anywhere;
}
.ua-lifecycle {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.ua-awaiting {
  margin: 0 auto 0 0;
  font-size: 12px;
  color: var(--text-secondary);
}
.ua-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
  padding-top: 10px;
  border-top: 1px solid var(--border-subtle);
}
.ua-confirm {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-right: auto;
  color: var(--text-secondary);
  font-size: 11.5px;
}
.ua-confirm input {
  accent-color: var(--accent);
}
.ua-btn {
  min-width: 92px;
  height: 34px;
  padding: 0 16px;
  border-radius: var(--r-md);
  border: 1px solid transparent;
  font-family: var(--sans);
  font-size: 13px;
  cursor: pointer;
  transition:
    transform var(--dur-press) var(--ease-1-out),
    background var(--dur-fast) var(--ease-1);
}
.ua-btn:active:not(:disabled) {
  transform: translateY(var(--shift-1, 1px));
}
.ua-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.ua-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.ua-btn.primary {
  background: var(--accent);
  color: var(--on-accent);
}
.ua-btn.primary:hover:not(:disabled) {
  background: var(--accent-hover);
}
.ua-btn.quiet {
  background: transparent;
  color: var(--text-primary);
  border-color: var(--border-strong);
}
.ua-btn.quiet:hover:not(:disabled) {
  background: var(--bg-inset);
}
.ua-btn.later {
  background: transparent;
  color: var(--text-secondary);
  border: 1px dashed var(--border-strong);
}
.ua-btn.later:hover:not(:disabled) {
  color: var(--text-primary);
  border-color: var(--text-muted);
}
.ua-note {
  margin: 0;
  color: var(--text-muted);
  font-size: 10.5px;
  line-height: 1.5;
}
</style>
