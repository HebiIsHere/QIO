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
const operations = ref<LedgerOperationView[]>([]);
const results = ref<IntentResultView[]>([]);
const loading = ref(false);
const error = ref("");

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
const authorization = computed(() => {
  const raw = props.intent?.authorization;
  return raw && typeof raw === "object" ? Object.entries(raw) : [];
});
const retry = computed(() => props.intent?.retry ?? null);
const retryText = computed(() => {
  if (!retry.value) return "没有重试记录";
  const attempts = retry.value.attempts ?? 0;
  const limit = retry.value.limit ?? 3;
  return `${statusLabel(retry.value.status)} · 已自动重试 ${attempts} / ${limit} 次`;
});

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
 * 读取：代次 + 目的意图双重校验，任何迟到 / 过期响应都不落到界面上。
 * ------------------------------------------------------------------------ */
let loadGeneration = 0;

function isCurrentLoad(mine: number, targetIntentId: string): boolean {
  return mine === loadGeneration && props.open && intentId.value === targetIntentId;
}

async function load(): Promise<void> {
  const mine = ++loadGeneration;
  const targetIntentId = intentId.value;
  const boardId = props.boardId;
  // 先清空上一个任务的详情、错误与结果：新标题下不显示旧内容，只显示当前读取状态。
  operations.value = [];
  results.value = [];
  error.value = "";
  loading.value = false;
  if (!props.open || !targetIntentId || !boardId) return;
  loading.value = true;
  try {
    const [ledger, result] = await Promise.all([
      fetchLedger(boardId),
      fetchIntentResults(boardId),
    ]);
    if (!isCurrentLoad(mine, targetIntentId)) return;
    operations.value = (ledger.operations ?? []).filter(
      (item) => !item.intent_id || item.intent_id === targetIntentId,
    );
    results.value = (result.results ?? []).filter(
      (item) => !item.intent_id || item.intent_id === targetIntentId,
    );
  } catch (cause) {
    if (!isCurrentLoad(mine, targetIntentId)) return;
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    if (isCurrentLoad(mine, targetIntentId)) loading.value = false;
  }
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

function ids(values: string[] | undefined): string {
  return values?.length ? values.join("、") : "未记录";
}
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
    <p class="erp-title">{{ intent.title }}</p>
    <p class="erp-status" :data-status="intent.status">状态：{{ statusLabel(intent.status) }}</p>
    <p v-if="intent.goal" class="erp-line"><strong>目标：</strong>{{ intent.goal }}</p>
    <p v-if="intent.expected" class="erp-line"><strong>预期：</strong>{{ intent.expected }}</p>
    <p v-if="intent.summary || intent.outcome" class="erp-line" data-im="execution-record-outcome">
      <strong>真实结果：</strong>{{ intent.summary || intent.outcome }}
    </p>

    <section class="erp-block">
      <h3>确认材料与版本</h3>
      <ul v-if="intent.basis?.length">
        <li v-for="item in intent.basis" :key="item.card_id">
          {{ item.card_id }} · 版本 {{ item.version }}
        </li>
      </ul>
      <p v-else class="erp-muted">没有记录确认材料。</p>
    </section>

    <section class="erp-block">
      <h3>授权范围</h3>
      <p>可读：{{ ids(scope.read_card_ids) }}</p>
      <p>可写：{{ ids(scope.write_card_ids) }}</p>
      <p>组：{{ ids(scope.group_ids) }}</p>
      <p>允许操作：{{ ids(scope.allowed_actions) }}</p>
      <p v-if="authorization.length" class="erp-muted">
        授权快照：{{ authorization.map(([key, value]) => key + "=" + String(value)).join(" · ") }}
      </p>
    </section>

    <section class="erp-block">
      <h3>实际操作</h3>
      <p v-if="loading" class="erp-muted" data-im="execution-record-loading">正在读取操作记录…</p>
      <div v-else-if="error" class="erp-error" role="alert" data-im="execution-record-error">
        <p>读取失败：{{ error }}</p>
        <button class="erp-retry" type="button" data-im="execution-record-retry" @click="load">重试</button>
      </div>
      <ul v-else-if="operations.length">
        <li v-for="item in operations" :key="item.operation_id || item.operation_key || item.action">
          {{ item.action || "未命名操作" }} · {{ item.status || "状态未记录" }}
          <span v-if="item.operation_key" class="erp-muted">（{{ item.operation_key }}）</span>
        </li>
      </ul>
      <p v-else class="erp-muted">没有已记录的操作。</p>
    </section>

    <section class="erp-block">
      <h3>结果</h3>
      <p v-if="loading" class="erp-muted">正在读取结果…</p>
      <p v-else-if="error" class="erp-muted" data-im="execution-record-results-error">
        读取失败，暂时没有可显示的结果（不能当作「没有结果」）。
      </p>
      <ul v-else-if="results.length">
        <li v-for="item in results" :key="item.result_id">
          {{ item.title || item.kind || "结果" }}<span v-if="item.content">：{{ item.content }}</span>
        </li>
      </ul>
      <p v-else class="erp-muted">没有已保存的结果卡。</p>
    </section>

    <section class="erp-block">
      <h3>失败、重试与恢复</h3>
      <p>{{ retryText }}</p>
      <p v-if="retry?.last_error" class="erp-error">最近失败原因：{{ retry.last_error }}</p>
      <p v-if="retry?.next_action" class="erp-muted">下一步：{{ retry.next_action }}</p>
    </section>
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
.erp {
  position: absolute;
  inset: 5% 10%;
  z-index: calc(var(--im-z-dialog) + 25);
  overflow: auto;
  padding: var(--sp-5);
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-3);
  color: var(--text-primary);
  font-family: var(--sans);
}
.erp:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.erp-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
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
@media (max-width: 720px) {
  .erp { inset: 3% 3%; padding: var(--sp-3); }
}
</style>
