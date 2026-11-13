<!--
  执行记录浮层（互动板意图卡 / 任务中心入口打开）。

  界面性质：**阻挡背景的浮层**（role=dialog + aria-modal=true，覆盖在板面/任务面板之上）。
  因此按共用弹窗语义补齐完整键盘操作，但不改外观：
    * 打开时把键盘移入浮层，Tab 在层内循环（不跑到后面的板面/对话）；
    * Esc 走 composables/useEscapeLayer 的共用层级栈，只关当前最内层（与 QModal/QCombo 同一套规则）；
    * 关闭后焦点回打开前的入口；入口已被移除时退回任务入口等仍在的可见位置；
    * 透明遮罩挡住背景点击，板面上不会因此误选卡片 / 误触按钮。

  读取隔离（问题 8）：切换意图、关闭后重开、快速连续切换时，先清空上一个意图的操作与结果，
  只保留当前意图的读取状态；响应回来时再用「代次 + 当前 intent_id + open」三重校验，
  迟到的旧响应一律丢弃 —— 标题与内容始终属于同一任务，不会把 A 的结果短暂显示在 B 的标题下。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { getActivePinia } from "pinia";
import { useEscapeLayer } from "../../composables/useEscapeLayer";
import { useModalArbiterStore } from "../../stores/modalArbiter";
import { fetchIntentResults, fetchLedger } from "../../services/interactive";
import { statusLabel } from "../../interactive/intentWorkbench";
import type {
  IntentResultView,
  IntentView,
  LedgerOperationView,
} from "../../interactive/intentWorkbench";
import {
  actionLabel,
  authorizationLines as authorizationReadableLines,
  basisSummaryLine,
  diagnosticJson,
  operationStatusLabel,
  readableValue,
  resultContentDisplay,
  resultKindLabel,
  retryStatusLabel,
  scopeLines as scopeReadableLines,
} from "../../interactive/executionRecordDisplay";

const props = withDefaults(
  defineProps<{
    intent: IntentView | null;
    boardId: string;
    open?: boolean;
  }>(),
  { open: false },
);
const emit = defineEmits<{ (e: "close"): void }>();

/** 与中央弹窗同一套仲裁身份：执行记录是只读的浮层，kind=other、允许直接关闭。 */
const MODAL_ID = "execution-record";
const arbiter = getActivePinia() ? useModalArbiterStore() : null;
const arbiterBlocked = ref(false);

const panelRef = ref<HTMLElement | null>(null);

/*
 * 操作（账本）与成果（结果卡）是两个独立读取：各自的 loading / error / 不可用 /
 * 数据 / 重试按钮。单边 503 不能把另一侧也清空，也不能把「读取失败」说成「没有数据」。
 */
const operations = ref<LedgerOperationView[]>([]);
const operationsLoading = ref(false);
const operationsError = ref("");
const operationsUnavailable = ref("");
const results = ref<IntentResultView[]>([]);
const resultsLoading = ref(false);
const resultsError = ref("");
const resultsUnavailable = ref("");

const intentId = computed(() => props.intent?.intent_id ?? "");
/** 真正可以显示并接收键盘：父级要求打开 + 仲裁放行 + 是当前活动层。 */
const visible = computed(
  () =>
    props.open &&
    Boolean(props.intent) &&
    !arbiterBlocked.value &&
    (arbiter ? arbiter.isVisible(MODAL_ID) : true),
);
const scope = computed(() => props.intent?.scope ?? {});
/** 授权范围只给「数量 + 可读名称」；原始 ID / 枚举 / 结构数据只在诊断里。 */
const scopeLines = computed(() => scopeReadableLines(scope.value));
const authorizationLines = computed(() => authorizationReadableLines(props.intent?.authorization));
const authorizationText = computed(() =>
  authorizationLines.value.map((line) => line.label + " " + line.value).join(" · "),
);
const retry = computed(() => props.intent?.retry ?? null);
const retryText = computed(() => {
  if (!retry.value) return "没有重试记录";
  const attempts = retry.value.attempts ?? 0;
  const limit = retry.value.limit ?? 3;
  return `${retryStatusLabel(retry.value.status)} · 已自动重试 ${attempts} / ${limit} 次`;
});
/** 意图状态：已登记状态给中文；未登记状态不回显内部枚举（原文在诊断里）。 */
const intentStatusText = computed(() => {
  const raw = typeof props.intent?.status === "string" ? props.intent.status.trim() : "";
  if (!raw) return "状态未记录";
  const label = statusLabel(raw);
  return label === raw ? "状态未记录" : label;
});
const outcomeText = computed(() => {
  const view = props.intent;
  if (!view) return "";
  return readableValue(view.summary).trim() || readableValue(view.outcome).trim();
});

function resultTitle(item: IntentResultView): string {
  return readableValue(item.title).trim() || resultKindLabel(item.kind);
}

function resultSummary(item: IntentResultView): string {
  return resultContentDisplay(item.content).text;
}

/* --------------------------------------------------------------------------
 * 仲裁登记：被更高优先级弹窗挡住时不显示（等它关闭后再出现），与 QModal 同规则。
 * 没有 pinia 的纯视觉环境 / 单测直接按 props 显示。
 * ------------------------------------------------------------------------ */
function syncArbiter(): void {
  if (!arbiter) return;
  if (!props.open || !props.intent) {
    if (arbiter.isOpen(MODAL_ID)) arbiter.close(MODAL_ID);
    arbiterBlocked.value = false;
    return;
  }
  if (arbiter.isOpen(MODAL_ID)) {
    arbiter.open({ id: MODAL_ID, kind: "other", dismissible: true });
    arbiterBlocked.value = false;
    return;
  }
  arbiterBlocked.value = !arbiter.open({ id: MODAL_ID, kind: "other", dismissible: true });
}

watch(
  [
    () => props.open,
    () => Boolean(props.intent),
    () => arbiter?.activeId ?? null,
    () => arbiter?.suspendedBy ?? null,
  ],
  syncArbiter,
  { immediate: true },
);

/* --------------------------------------------------------------------------
 * 读取：记录身份（intentId + boardId + open）+ 每次读取的代次与单侧尝试号校验。
 *
 * 操作与成果分别读取、分别失败、分别重试；任何迟到 / 过期响应都只丢弃自己，
 * 不落到界面上（也不会让另一侧的读取状态跟着变）。
 * ------------------------------------------------------------------------ */
interface ReadTarget {
  generation: number;
  intentId: string;
  boardId: string;
}

let loadGeneration = 0;
let operationsAttempt = 0;
let resultsAttempt = 0;

function errorText(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}

/** 只有「同一轮读取 + 同一记录 + 面板仍打开 + 该侧还是最新一次尝试」才允许写回。 */
function isCurrentRead(target: ReadTarget, attempt: number, side: "operations" | "results"): boolean {
  if (target.generation !== loadGeneration) return false;
  if (!props.open) return false;
  if (intentId.value !== target.intentId) return false;
  if (props.boardId !== target.boardId) return false;
  return side === "operations" ? attempt === operationsAttempt : attempt === resultsAttempt;
}

/** 切记录 / 重开 / 关面板：先作废上一轮所有在途读取，并立即清空旧内容。 */
function beginReadCycle(): ReadTarget {
  loadGeneration += 1;
  operationsAttempt += 1;
  resultsAttempt += 1;
  operations.value = [];
  operationsLoading.value = false;
  operationsError.value = "";
  operationsUnavailable.value = "";
  results.value = [];
  resultsLoading.value = false;
  resultsError.value = "";
  resultsUnavailable.value = "";
  return { generation: loadGeneration, intentId: intentId.value, boardId: props.boardId };
}

function currentTarget(): ReadTarget {
  return { generation: loadGeneration, intentId: intentId.value, boardId: props.boardId };
}

async function loadOperations(target: ReadTarget): Promise<void> {
  if (!props.open || !target.intentId || !target.boardId) return;
  const attempt = ++operationsAttempt;
  operationsLoading.value = true;
  operationsError.value = "";
  operationsUnavailable.value = "";
  try {
    const ledger = await fetchLedger(target.boardId);
    if (!isCurrentRead(target, attempt, "operations")) return;
    if (ledger.available === false) {
      operations.value = [];
      operationsUnavailable.value = ledger.reason || "服务端没有说明原因";
      return;
    }
    operations.value = (ledger.operations ?? []).filter(
      (item) => !item.intent_id || item.intent_id === target.intentId,
    );
  } catch (cause) {
    if (!isCurrentRead(target, attempt, "operations")) return;
    operations.value = [];
    operationsError.value = errorText(cause);
  } finally {
    if (isCurrentRead(target, attempt, "operations")) operationsLoading.value = false;
  }
}

async function loadResults(target: ReadTarget): Promise<void> {
  if (!props.open || !target.intentId || !target.boardId) return;
  const attempt = ++resultsAttempt;
  resultsLoading.value = true;
  resultsError.value = "";
  resultsUnavailable.value = "";
  try {
    const result = await fetchIntentResults(target.boardId);
    if (!isCurrentRead(target, attempt, "results")) return;
    if (result.available === false) {
      results.value = [];
      resultsUnavailable.value = result.reason || "服务端没有说明原因";
      return;
    }
    results.value = (result.results ?? []).filter(
      (item) => !item.intent_id || item.intent_id === target.intentId,
    );
  } catch (cause) {
    if (!isCurrentRead(target, attempt, "results")) return;
    results.value = [];
    resultsError.value = errorText(cause);
  } finally {
    if (isCurrentRead(target, attempt, "results")) resultsLoading.value = false;
  }
}

/** 进入一条记录：作废旧在途读取、清空旧内容，两侧各自独立发起。 */
async function load(): Promise<void> {
  const target = beginReadCycle();
  if (!props.open || !target.intentId || !target.boardId) return;
  await Promise.allSettled([loadOperations(target), loadResults(target)]);
}

/** 重试只重读对应一侧：另一侧的状态与内容原样保留。 */
function retryOperations(): void {
  if (operationsLoading.value) return;
  void loadOperations(currentTarget());
}

function retryResults(): void {
  if (resultsLoading.value) return;
  void loadResults(currentTarget());
}

watch(
  () => [props.open, intentId.value, props.boardId] as const,
  () => void load(),
  { immediate: true },
);

/* --------------------------------------------------------------------------
 * 键盘与焦点：打开移入、Tab 留在层内、关闭归还。
 * Esc 交给共用层级栈（后打开的内层优先），不在这里另挂 window 监听。
 * ------------------------------------------------------------------------ */
let restoreFocusTo: HTMLElement | null = null;
let movedFocus = false;
let closeEmitted = false;

function requestClose(): void {
  if (!visible.value || closeEmitted) return;
  closeEmitted = true;
  emit("close");
}

useEscapeLayer({
  id: MODAL_ID + "-escape",
  active: () => visible.value,
  onEscape: () => {
    if (!visible.value) return false;
    requestClose();
    return true;
  },
});

function focusablesInPanel(): HTMLElement[] {
  const root = panelRef.value;
  if (!root) return [];
  return [...root.querySelectorAll<HTMLElement>(
    'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )].filter((element) => element.getAttribute("aria-hidden") !== "true");
}

function isEditableTarget(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element || typeof element.tagName !== "string") return false;
  return (
    /^(INPUT|TEXTAREA|SELECT)$/.test(element.tagName) || element.isContentEditable === true
  );
}

function trapTab(event: KeyboardEvent): void {
  const root = panelRef.value;
  if (!root) return;
  const list = focusablesInPanel();
  if (!list.length) {
    event.preventDefault();
    root.focus({ preventScroll: true });
    return;
  }
  const first = list[0];
  const last = list[list.length - 1];
  const active = document.activeElement as HTMLElement | null;
  if (!active || !root.contains(active)) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
    return;
  }
  if (event.shiftKey && active === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

/**
 * 浮层打开期间：Tab 在层内循环；Delete / Backspace / Ctrl(Cmd)+Z/Y 这类
 * 「后面板面也会响应」的键不再穿透到背景（输入框里正常编辑时不拦截）。
 */
function onKeydownCapture(event: KeyboardEvent): void {
  if (!visible.value) return;
  if (event.key === "Tab") {
    trapTab(event);
    return;
  }
  if (isEditableTarget(event.target)) return;
  const withCommand = event.ctrlKey || event.metaKey;
  const key = event.key.toLowerCase();
  if (
    event.key === "Delete" ||
    event.key === "Backspace" ||
    (withCommand && (key === "z" || key === "y"))
  ) {
    event.preventDefault();
    event.stopPropagation();
  }
}

function restoreFocus(): void {
  if (!movedFocus) return;
  movedFocus = false;
  const target = restoreFocusTo;
  restoreFocusTo = null;
  if (typeof document === "undefined") return;
  if (target && target.isConnected) {
    target.focus();
    return;
  }
  // 入口随操作消失（记录行被刷新掉等）：退回互动页上仍在的可见入口，别让键盘从头 Tab。
  const fallback = document.querySelector<HTMLElement>(
    '[data-im="tasks-entry"], [data-im="board-toolbar"] button:not([disabled]), main button:not([disabled])',
  );
  fallback?.focus();
}

/* 打开时记录入口焦点；真正关闭（open 变 false / 卸载）时才归还。 */
watch(
  () => props.open,
  (open, wasOpen) => {
    if (typeof document === "undefined") return;
    if (open && !wasOpen) {
      closeEmitted = false;
      const active = document.activeElement;
      restoreFocusTo =
        active instanceof HTMLElement && active !== document.body ? active : null;
      return;
    }
    if (!open && wasOpen) restoreFocus();
  },
  { immediate: true },
);

/*
 * 键盘监听跟随「真正活动」：被更高优先级弹窗盖住时解绑（不抢 Esc/Tab），
 * 回到活动层时重新绑定并把焦点放回层内；这只是让位，不是关闭，不能在这里归还焦点。
 */
watch(
  visible,
  async (on) => {
    if (typeof window === "undefined" || typeof document === "undefined") return;
    if (on) {
      window.addEventListener("keydown", onKeydownCapture, true);
      await nextTick();
      if (!visible.value) return;
      const panel = panelRef.value;
      if (panel && !panel.contains(document.activeElement)) {
        movedFocus = true;
        panel.focus({ preventScroll: true });
      }
      return;
    }
    window.removeEventListener("keydown", onKeydownCapture, true);
  },
  { immediate: true },
);

onBeforeUnmount(() => {
  if (typeof window !== "undefined") {
    window.removeEventListener("keydown", onKeydownCapture, true);
  }
  if (arbiter?.isOpen(MODAL_ID)) arbiter.close(MODAL_ID);
  restoreFocus();
});
</script>

<template>
  <!-- 透明遮罩：挡住背景点击（不改变任何外观），只关闭本层。 -->
  <div
    v-if="visible"
    class="erp-scrim"
    data-im="execution-record-scrim"
    aria-hidden="true"
    @pointerdown.stop
    @click="requestClose"
  ></div>
  <section
    v-if="visible && intent"
    ref="panelRef"
    class="erp"
    data-im="execution-record"
    role="dialog"
    aria-modal="true"
    aria-label="执行记录"
    tabindex="-1"
  >
    <header class="erp-head">
      <h2>执行记录</h2>
      <button class="erp-close" type="button" data-im="execution-record-close" aria-label="关闭执行记录" @click="requestClose">×</button>
    </header>
    <!-- #91：标题栏与关闭按钮留在顶部，只有这段正文滚动（见样式 .erp / .erp-body）。 -->
    <div class="erp-body" data-im="execution-record-body">
    <p class="erp-title">{{ readableValue(intent.title) }}</p>
    <p class="erp-status" :data-status="intent.status" data-im="execution-record-status">状态：{{ intentStatusText }}</p>
    <p v-if="intent.goal" class="erp-line"><strong>目标：</strong>{{ readableValue(intent.goal) }}</p>
    <p v-if="intent.expected" class="erp-line"><strong>预期：</strong>{{ readableValue(intent.expected) }}</p>
    <p v-if="outcomeText" class="erp-line" data-im="execution-record-outcome">
      <strong>真实结果：</strong>{{ outcomeText }}
    </p>

    <section class="erp-block" data-im="execution-record-basis">
      <h3>确认材料与版本</h3>
      <p v-if="intent.basis?.length" data-im="execution-record-basis-summary">
        {{ basisSummaryLine(intent.basis) }}
      </p>
      <p v-else class="erp-muted">没有记录确认材料。</p>
    </section>

    <section class="erp-block" data-im="execution-record-scope">
      <h3>授权范围</h3>
      <p v-for="line in scopeLines" :key="line.label">{{ line.label }}：{{ line.value }}</p>
      <p class="erp-muted" data-im="execution-record-authorization">授权快照：{{ authorizationText }}</p>
    </section>

    <section class="erp-block">
      <h3>实际操作</h3>
      <p v-if="operationsLoading" class="erp-muted" data-im="execution-record-loading">正在读取操作记录…</p>
      <div v-else-if="operationsError" class="erp-error" role="alert" data-im="execution-record-error">
        <p>读取失败：{{ operationsError }}</p>
        <!-- 失败态与空态已经分行显示（空态只在读取成功且确实为空时出现），
             不再补一句「失败不等于没有」的判据解释。 -->
        <button class="erp-retry" type="button" data-im="execution-record-retry" @click="retryOperations">重试读取操作记录</button>
      </div>
      <div v-else-if="operationsUnavailable" class="erp-error" role="alert" data-im="execution-record-unavailable">
        <p>操作账本暂时不可用：{{ operationsUnavailable }}</p>
        <button class="erp-retry" type="button" data-im="execution-record-retry" @click="retryOperations">重试读取操作记录</button>
      </div>
      <ul v-else-if="operations.length" data-im="execution-record-operations">
        <li v-for="item in operations" :key="item.operation_id || item.operation_key || item.action">
          {{ actionLabel(item.action) }} · {{ operationStatusLabel(item.status) }}
          <!-- 内部 operation_key 属调试字段：只留在下方「诊断」的原始条目里，主界面不显示。 -->
        </li>
      </ul>
      <p v-else class="erp-muted">没有已记录的操作。</p>
    </section>

    <section class="erp-block">
      <h3>结果</h3>
      <p v-if="resultsLoading" class="erp-muted" data-im="execution-record-results-loading">正在读取结果…</p>
      <div v-else-if="resultsError" class="erp-error" role="alert" data-im="execution-record-results-error">
        <p>读取失败：{{ resultsError }}</p>
        <button class="erp-retry" type="button" data-im="execution-record-results-retry" @click="retryResults">重试读取结果</button>
      </div>
      <div v-else-if="resultsUnavailable" class="erp-error" role="alert" data-im="execution-record-results-unavailable">
        <p>结果暂时不可用：{{ resultsUnavailable }}</p>
        <button class="erp-retry" type="button" data-im="execution-record-results-retry" @click="retryResults">重试读取结果</button>
      </div>
      <ul v-else-if="results.length" data-im="execution-record-results">
        <li v-for="item in results" :key="item.result_id">
          {{ resultTitle(item) }}<span v-if="resultSummary(item)">：{{ resultSummary(item) }}</span>
        </li>
      </ul>
      <p v-else class="erp-muted">没有已保存的结果卡。</p>
    </section>

    <section class="erp-block" data-im="execution-record-retry-facts">
      <h3>失败、重试与恢复</h3>
      <p>{{ retryText }}</p>
      <p v-if="retry?.last_error" class="erp-error">最近失败原因：{{ readableValue(retry.last_error) }}</p>
      <p v-if="retry?.next_action" class="erp-muted">下一步：{{ readableValue(retry.next_action) }}</p>
    </section>

    <!-- 诊断：原始 ID / 内部枚举 / 结构数据只在这里（默认折叠，可展开核对）。 -->
    <details class="erp-diagnostics" data-im="execution-record-diagnostics">
      <summary>诊断（原始标识与结构数据）</summary>
      <dl class="erp-diagnostics-list mono">
        <dt>意图标识</dt><dd>{{ readableValue(intent.intent_id) || "未记录" }}</dd>
        <dt>板面标识</dt><dd>{{ readableValue(props.boardId) || "未记录" }}</dd>
        <dt>意图版本</dt><dd>{{ readableValue(intent.version) || "未记录" }}</dd>
        <dt>意图状态原文</dt><dd>{{ diagnosticJson(intent.status) }}</dd>
        <dt>允许操作原文</dt><dd><pre>{{ diagnosticJson(scope.allowed_actions) }}</pre></dd>
        <dt>授权范围原文</dt><dd><pre>{{ diagnosticJson(scope) }}</pre></dd>
        <dt>授权原文</dt><dd><pre>{{ diagnosticJson(intent.authorization) }}</pre></dd>
        <dt>确认材料原文</dt><dd><pre>{{ diagnosticJson(intent.basis) }}</pre></dd>
        <dt>操作原始条目</dt><dd><pre>{{ diagnosticJson(operations) }}</pre></dd>
        <dt>结果原始条目</dt><dd><pre>{{ diagnosticJson(results) }}</pre></dd>
        <dt>重试原文</dt><dd><pre>{{ diagnosticJson(retry) }}</pre></dd>
      </dl>
    </details>
    </div>
  </section>
</template>

<style scoped>
/*
  层级：执行记录是阻挡背景的浮层，必须压在任务中心（--im-z-dialog + 10）与
  悬浮对话区（+ 20）之上；影响确认 / 审批仍由统一层负责更高层。
*/
.erp-scrim {
  position: absolute;
  inset: 0;
  z-index: calc(var(--im-z-dialog) + 24);
  background: transparent;
}
/*
  #91：浮层本身不滚动。标题栏（含关闭按钮）固定在顶部，标题下面的正文
  在自己的滚动容器里 —— 滚到记录底部时关闭按钮仍在原处可点。
*/
.erp {
  position: absolute;
  inset: 5% 10%;
  z-index: calc(var(--im-z-dialog) + 25);
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  padding: 0;
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-3);
  color: var(--text-primary);
  font-family: var(--sans);
}
.erp:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
/* 固定的标题栏：flex-shrink:0，不参与正文滚动 */
.erp-head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: var(--sp-5) var(--sp-5) var(--sp-3);
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-elevated);
}
/* 唯一滚动容器：正文可以很长，但标题栏不跟着走 */
.erp-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 0 var(--sp-5) var(--sp-5);
}
.erp-head h2 { margin: 0; font-family: var(--serif); font-size: var(--fs-lg); }
.erp-close {
  width: 28px;
  height: 28px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  background: var(--bg-base);
  color: var(--text-secondary);
  cursor: pointer;
}
.erp-title { margin: var(--sp-3) 0 var(--sp-1); font-size: var(--fs-base); font-weight: 600; }
.erp-status { margin: 0 0 var(--sp-3); color: var(--text-secondary); font-size: var(--fs-sm); }
.erp-line { margin: var(--sp-1) 0; font-size: var(--fs-sm); line-height: 1.6; }
.erp-block { margin-top: var(--sp-4); padding-top: var(--sp-3); border-top: 1px solid var(--border-subtle); }
.erp-block h3 { margin: 0 0 var(--sp-2); font-size: var(--fs-sm); color: var(--text-strong); }
.erp-block p { margin: var(--sp-1) 0; font-size: var(--fs-sm); line-height: 1.6; overflow-wrap: anywhere; }
.erp-block ul { margin: 0; padding-left: 1.2em; font-size: var(--fs-sm); line-height: 1.7; overflow-wrap: anywhere; }
.erp-muted { color: var(--text-muted); }
.erp-error { color: var(--danger); }
.erp-error p { margin: 0 0 var(--sp-1); }
.erp-retry {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-base);
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  padding: 1px var(--sp-2);
  cursor: pointer;
}
.erp-retry:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
/* 诊断：默认折叠，展开后原文可核对；结构数据不内嵌进主文本。 */
.erp-diagnostics {
  margin-top: var(--sp-4);
  padding-top: var(--sp-3);
  border-top: 1px solid var(--border-subtle);
  font-size: var(--fs-xs);
}
.erp-diagnostics > summary {
  cursor: pointer;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}
.erp-diagnostics > summary:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.erp-diagnostics-list {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: var(--sp-1) var(--sp-3);
  margin: var(--sp-2) 0 0;
}
.erp-diagnostics-list dt { color: var(--text-muted); }
.erp-diagnostics-list dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.erp-diagnostics-list pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--mono);
  font-size: var(--fs-xs);
}
@media (max-width: 720px) {
  .erp { inset: 3% 3%; }
  .erp-head { padding: var(--sp-3) var(--sp-3) var(--sp-2); }
  .erp-body { padding: 0 var(--sp-3) var(--sp-3); }
}
</style>
