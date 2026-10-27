<!--
  F01：保存前的影响确认 + 剩余撤回决定（B 组，本轮从旧 ImpactConfirmDialog 迁移能力）。

  基线缺陷：
  1) InteractiveView 只挂了统一审批层，store.saveNow 产生的 pendingImpact 没有任何界面承接，
     用户看到「有改动尚未保存」却永远没有确认入口（死等待）；
  2) 旧 ImpactConfirmDialog 已实现但没有挂载：它的**剩余撤回决定**能力（pendingDecision、
     剩余撤回、明确 decisionIds、关闭不撤回、再次查看、失败重试、部分完成反馈）在新页面上完全不可达。

  这个组件是互动页上**唯一**的影响确认面，也是剩余撤回决定的承接界面：
  - 影响确认：明确展示真实受影响任务、材料与操作后果，并写明「等待确认，尚未保存」；
    两个决定入口：确认改动（保存并暂停相关任务）/ 取消本次改动（不保存、任务继续）；
    结果只按 store 的真实结果说话（保存回执 / 预判失败 / 保存失败 / 被新改动接手）；
  - 剩余撤回（迁移自旧 ImpactConfirmDialog）：任务失败或取消后，服务端仍留着
    revert.pendingDecision（会影响其他工作、停下等用户决定）。这里按**任务身份**展示剩余项，
    「继续」只提交这次界面明确展示的 decisionIds（服务端会按当前内容重核），
    失败原样显示真实原因并可重试；「取消」只结束本次提示（不执行任何撤回），
    之后通过「再次查看」入口重新打开；部分完成会说明还剩多少项；
  - 与工具/权限审批共用「同时只有一个阻塞确认」：审批弹窗打开时让位，关闭后自动回来；
  - 键盘：打开时聚焦第一个按钮、Tab 限制在框内、Escape = 影响确认「取消本次改动」/ 撤回「关闭提示」。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { useModalArbiterStore } from "../../../stores/modalArbiter";
import type { ImpactConfirmOutcome, Intent } from "../../../interactive/types";

const store = useInteractiveStore();
/** 中央弹窗仲裁（B 的公共接口）：影响确认是阻塞决定的中央窗口，必须登记，不能靠 z-index 抢层。 */
const arbiter = useModalArbiterStore();
const ARBITER_ID = "impact-confirm";

/* ── 影响确认（保存前）────────────────────────────────────── */
const impactOpen = computed(() => Boolean(store.pendingImpact));
const affected = computed(() => store.pendingImpact?.affected ?? []);
const note = computed(() => String(store.pendingImpact?.note ?? "").trim());

/* ── 剩余撤回决定（迁移自旧 ImpactConfirmDialog，N3/N4）────────
 * 判据与服务端事实一致：revert.pendingDecision 非空、任务仍在（运行/暂停/失败/取消）状态；
 * 被用户主动结束提示（dismissed）的不再自动打断，只在「再次查看」入口里可见。
 */
const REVERT_STATUSES: string[] = ["running", "paused", "failed", "cancelled"];
function hasPendingDecision(intent: Intent): boolean {
  return (intent.revert?.pendingDecision?.length ?? 0) > 0 && REVERT_STATUSES.includes(intent.status);
}
const pendingReverts = computed<Intent[]>(() =>
  store.intents.filter((intent) => hasPendingDecision(intent) && !store.revertDecisionDismissed?.[intent.id]),
);
/** 已被用户结束提示、但服务端确实还有待决定项：不自动打断，只留可控的「再次查看」入口。 */
const dismissedReverts = computed<Intent[]>(() =>
  store.intents.filter((intent) => hasPendingDecision(intent) && Boolean(store.revertDecisionDismissed?.[intent.id])),
);

const mode = computed<"impact" | "revert" | null>(() => {
  if (impactOpen.value) return "impact";
  return pendingReverts.value.length ? "revert" : null;
});
const open = computed(() => mode.value !== null);

/**
 * 仲裁状态：
 * - arbiterBlocked：我们想打开，但已有同/更高优先级的中央弹窗（关闭询问 / 引导等），本次不显示；
 * - suspendedBy：整窗级触发器（关闭询问）把业务弹窗整体挂起 —— 只隐藏，DOM/状态不丢；
 * - coveredByOther：我们打开后又有更高优先级的弹窗叠上来 —— 让位，但保留状态。
 * 三者任一成立都只是「暂不显示」，不是取消这次决定；恢复到能显示时自动回到这里。
 */
const arbiterBlocked = ref(false);
/** 整体挂起（关闭询问等）的挂起方：只用于说明「为什么现在不显示」。 */
const suspendedBy = computed(() => (open.value ? arbiter.hiddenBy(ARBITER_ID) : null));
/** 被更高优先级窗口盖住的那一方（本窗口已登记但不是栈顶活动项）。 */
const coveredByOther = computed(() => {
  if (!open.value || !arbiter.isOpen(ARBITER_ID)) return "";
  const activeId = arbiter.activeId;
  return activeId && activeId !== ARBITER_ID ? activeId : "";
});
/**
 * 统一活动判断（F01，契约 §3.1）：已打开 && 无整体挂起 && 是栈顶活动项。
 * 覆盖三种让位：注册被拒、被更高优先级盖住、被 suspendAll 整体挂起。
 * 只 v-show 隐藏：DOM、未提交的选择与状态都保留。
 */
const visible = computed(() => open.value && arbiter.isActive(ARBITER_ID));
/** 暂时让位（不取消这次决定）：给用户一句原因，条件恢复后自动回来。 */
const deferred = computed(() => open.value && !visible.value);
const deferredText = computed(() => {
  const waiting = mode.value === "revert" ? "撤回决定" : "影响确认";
  if (suspendedBy.value || arbiterBlocked.value || coveredByOther.value) {
    return (
      waiting + "还在等你决定，但此刻有更高优先级的窗口（如关闭确认 / 引导 / 审批）正开着：" +
      "同一时刻只显示一个中央窗口，这里不会叠上去；处理完那个窗口后会回到" + waiting + "。"
    );
  }
  return "";
});

/** 按当前模式把窗口登记进仲裁（幂等）：被挡住时如实记录，条件恢复后自动再试。 */
function syncArbiter(): void {
  if (!open.value) {
    if (arbiter.isOpen(ARBITER_ID)) arbiter.close(ARBITER_ID);
    arbiterBlocked.value = false;
    return;
  }
  const kind = mode.value === "revert" ? "recovery" : "impact";
  if (arbiter.isOpen(ARBITER_ID)) {
    // 模式可能从影响确认切到剩余撤回：只更新描述，不重复入栈
    arbiter.open({ id: ARBITER_ID, kind, dismissible: false });
    arbiterBlocked.value = false;
    return;
  }
  arbiterBlocked.value = !arbiter.open({ id: ARBITER_ID, kind, dismissible: false });
}

watch(
  [open, mode, () => arbiter.count, () => arbiter.activeId, () => arbiter.suspendedBy],
  syncArbiter,
  { immediate: true },
);
onBeforeUnmount(() => {
  if (arbiter.isOpen(ARBITER_ID)) arbiter.close(ARBITER_ID);
});
const title = computed(() =>
  mode.value === "revert"
    ? "还有改动没有撤回：需要你决定剩下的部分"
    : "这次改动还没有生效：它会影响正在执行的任务",
);

/* ── 真实结果与局部状态 ──────────────────────────────────── */
type LayerOutcomeKind =
  | ImpactConfirmOutcome
  | "cancelled_unconfirmed"
  | "revert_done"
  | "revert_partial";
const outcome = ref<{ kind: LayerOutcomeKind; title: string; text: string; paused: Intent[] } | null>(null);
const busy = ref(false);
const retryBusy = ref(false);
const error = ref<string | null>(null);
/** 撤回模式里「部分完成 / 剩多少项」的说明（只在框还开着时可见） */
const notice = ref<string | null>(null);
/** 撤回模式里最近一次处理的真实失败原因（来自 store 的 revertDecisionStateFor 或返回值） */
const revertError = ref<string | null>(null);

function outcomeTitle(kind: string): string {
  switch (kind) {
    case "saved":
      return "改动已保存生效";
    case "needs_confirm":
      return "这次改动还没有保存";
    case "check_failed":
      return "这次改动还没有保存：保存前的影响预判没有完成";
    case "save_failed":
      return "这次改动没有保存成功";
    case "superseded":
      return "这次确认没有落库：已按最新板面重新核实";
    case "cancelled":
      return "已取消这次改动";
    case "revert_done":
      return "已处理完这个任务剩下的撤回";
    case "revert_partial":
      return "还有剩余撤回项等你决定";
    default:
      return "撤回还没有被服务器事实确认";
  }
}

/* ── 影响确认：确认 / 取消 / 重试（原有能力） ─────────────── */
async function confirm(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  error.value = null;
  try {
    const result = await store.confirmImpact();
    if (store.pendingImpact) {
      // 仍需重新确认（等待期间板面又变了 / 范围变化）：留在对话框，按新说明重新决定
      return;
    }
    const kind = String(result?.outcome ?? "superseded");
    const paused = result?.paused ?? [];
    const text =
      kind === "saved"
        ? "改动已保存生效；受影响的任务已暂停并保留进度（不会自动继续，需要你在任务面板里确认后按当前材料继续）。"
        : result?.reason || "这次改动没有落库，也没有任务被暂停";
    outcome.value = { kind: kind as ImpactConfirmOutcome, title: outcomeTitle(kind), text, paused };
  } catch (err) {
    const message = (err as Error).message || "确认没有完成";
    error.value = message;
    outcome.value = { kind: "save_failed", title: outcomeTitle("save_failed"), text: message, paused: [] };
  } finally {
    busy.value = false;
  }
}

async function cancel(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  error.value = null;
  try {
    await store.cancelImpact();
    const recovery = store.cancelRecovery;
    if (recovery?.active) {
      outcome.value = {
        kind: "cancelled_unconfirmed",
        title: outcomeTitle("cancelled_unconfirmed"),
        text: recovery.reason || "撤回还没有被服务器事实确认",
        paused: [],
      };
      return;
    }
    outcome.value = {
      kind: "cancelled",
      title: outcomeTitle("cancelled"),
      text: "已取消：这次改动没有生效，任务继续；其他独立任务不受影响。",
      paused: [],
    };
  } catch (err) {
    const message = (err as Error).message || "取消失败";
    error.value = message;
    outcome.value = {
      kind: "cancelled_unconfirmed",
      title: outcomeTitle("cancelled_unconfirmed"),
      text: message,
      paused: [],
    };
  } finally {
    busy.value = false;
  }
}

/** 预判失败 / 保存失败之后：按最新候选重新核实并保存（不会跳过影响确认）。 */
async function retrySave(): Promise<void> {
  if (retryBusy.value) return;
  retryBusy.value = true;
  error.value = null;
  try {
    await store.saveNow();
    if (store.pendingImpact) return;
    if (store.saveStatus === "saved") {
      outcome.value = {
        kind: "saved",
        title: outcomeTitle("saved"),
        text: "改动已保存生效。",
        paused: [...store.lastSavePaused],
      };
      return;
    }
    if (store.saveStatus === "error") {
      outcome.value = {
        kind: "save_failed",
        title: outcomeTitle("save_failed"),
        text: store.saveError || "保存没有完成",
        paused: [],
      };
      return;
    }
    if (store.impactCheckError) {
      outcome.value = {
        kind: "check_failed",
        title: outcomeTitle("check_failed"),
        text: store.impactCheckError,
        paused: [],
      };
    }
  } finally {
    retryBusy.value = false;
  }
}

/** 取消已经生效、但撤回还没被服务器事实确认：重新核对一次（只读，不保存）。 */
async function retryCancelRecovery(): Promise<void> {
  if (retryBusy.value) return;
  retryBusy.value = true;
  try {
    const result = await store.retryCancelRecovery();
    if (result.ok) {
      outcome.value = {
        kind: "cancelled",
        title: outcomeTitle("cancelled"),
        text: "已取消：这次改动没有生效，任务继续。",
        paused: [],
      };
    } else {
      outcome.value = {
        kind: "cancelled_unconfirmed",
        title: outcomeTitle("cancelled_unconfirmed"),
        text: result.error || store.cancelRecovery?.reason || "撤回还没有被服务器事实确认",
        paused: [],
      };
    }
  } finally {
    retryBusy.value = false;
  }
}

function dismissOutcome(): void {
  outcome.value = null;
}

/* ── 剩余撤回：明确 decisionIds / 关闭不撤回 / 再次查看 / 失败重试 ── */
/** 这次真正展示给用户的决定项（按任务身份，只取当前 pendingDecision 的 id）。 */
function shownDecisionIds(intent: Intent): string[] {
  return (intent.revert?.pendingDecision ?? []).map((item) => item.id);
}
/** 服务端事实刷新后这个任务还剩哪些决定项（成功返回后可能已经没有了）。 */
function currentDecisionIds(intentId: string): string[] {
  const fresh = store.intents.find((item) => item.id === intentId) ?? null;
  return fresh ? shownDecisionIds(fresh) : [];
}
function revertStateFor(intentId: string): { status: "idle" | "pending" | "error"; error: string | null } {
  return store.revertDecisionStateFor(intentId);
}

/**
 * 「继续」：只处理这次明确展示给这个任务的剩余决定项。
 * - 真的执行（带 decisionIds，服务端按当前内容重核）；
 * - 成功但仍有剩余 → 如实说还剩多少项（部分完成反馈）；
 * - 失败 → 显示服务端真实原因（store 同时记 error 状态），按钮恢复可点 = 可重试；
 * - 不重复执行：上一次还在飞时直接返回。
 */
async function continueRevert(intentId: string): Promise<void> {
  if (!intentId) return;
  if (revertStateFor(intentId).status === "pending") return;
  error.value = null;
  notice.value = null;
  revertError.value = null;
  const decisionIds = currentDecisionIds(intentId);
  try {
    const result = await store.continueRevertDecision(intentId, decisionIds);
    if (result?.ok) {
      // 以服务端的逐项结果为事实：处理了几项、哪些需要重新决定、还剩几项。
      // 老后端 / 测试替身没有 decision 摘要时，只按最新清单说剩余项，不拿展示条数冒充处理条数。
      const decision = result.decision;
      const remaining = decision?.remaining ?? currentDecisionIds(intentId);
      const parts: string[] = [];
      if (decision) {
        parts.push("已按你的决定撤回 " + decision.processed.length + " 项");
        if (decision.reconfirmed.length) {
          parts.push(decision.reconfirmed.length + " 项因为之后又有变化，需要你按最新说明重新决定");
        }
        if (decision.gone.length) parts.push(decision.gone.length + " 项已经不在板面上");
      } else {
        parts.push("这次撤回已经执行");
      }
      if (remaining.length) {
        const text = parts.join("；") + "；还有 " + remaining.length + " 项需要你决定。";
        notice.value = text;
        outcome.value = { kind: "revert_partial", title: outcomeTitle("revert_partial"), text, paused: [] };
      } else {
        const text = parts.join("；") + "；这个任务剩下的改动已经处理完。";
        notice.value = text;
        outcome.value = { kind: "revert_done", title: outcomeTitle("revert_done"), text, paused: [] };
      }
      return;
    }
    revertError.value = result?.reason || result?.detail || "这次撤回没有执行";
  } catch (err) {
    revertError.value = (err as Error).message || "这次撤回没有执行";
  }
}

/** 「取消」：只结束本次提示、保留板面，不执行任何撤回；之后可从「再次查看」重新打开。 */
function dismissReverts(): void {
  const targets = pendingReverts.value.map((intent) => intent.id);
  for (const id of targets) store.dismissRevertDecision(id);
  notice.value = null;
  error.value = null;
  revertError.value = null;
}

/** 用户主动再次查看某个任务的待决定项（服务端仍是真实未处理的决定）。 */
function reopenRevert(intentId: string): void {
  store.reopenRevertDecision(intentId);
}

/* ── 键盘与焦点：Escape 语义按模式区分，Tab 限制在框内 ────────── */
const dialogEl = ref<HTMLElement | null>(null);
const firstAction = ref<HTMLButtonElement | null>(null);
let lastFocused: HTMLElement | null = null;

function focusablesInDialog(): HTMLElement[] {
  const root = dialogEl.value;
  if (!root) return [];
  return [...root.querySelectorAll<HTMLElement>("button:not([disabled])")];
}

function onKeydown(event: KeyboardEvent): void {
  if (!visible.value) return;
  if (event.key === "Escape") {
    // 影响确认是**不可被外部 Esc 取消**的：Esc 若顺手撤销本次改动，等于用户按了一个键
    // 就把刚做的改动丢掉，且没有任何明确选择。只有「确认改动 / 取消本次改动」两个按钮
    // 能决定这次保存；Esc 在这里什么都不做（焦点仍留在框内，语义由两个按钮承担）。
    if (mode.value === "impact") {
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    // 剩余撤回不是保存决定：Esc = 结束这次提示（不执行任何撤回），与「取消」按钮同义。
    event.preventDefault();
    dismissReverts();
    return;
  }
  if (event.key !== "Tab") return;
  const list = focusablesInDialog();
  if (!list.length) return;
  const first = list[0];
  const last = list[list.length - 1];
  const active = document.activeElement as HTMLElement | null;
  // 焦点限制在框内：到边界就回卷，绝不跑到页面其它地方
  if (event.shiftKey && (active === first || !dialogEl.value?.contains(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (active === last || !dialogEl.value?.contains(active))) {
    event.preventDefault();
    first.focus();
  }
}

function bindKeyboard(on: boolean): void {
  if (typeof window === "undefined") return;
  if (on) window.addEventListener("keydown", onKeydown, true);
  else window.removeEventListener("keydown", onKeydown, true);
}

let keyboardBound = false;
/**
 * 键盘与焦点跟随「真正活动」：被更高优先级盖住 / 整体挂起时解绑监听（不抢 Esc/Tab），
 * 恢复活动时重新绑定并把焦点放回框内（不能让焦点留在页面外）。
 */
watch(
  visible,
  async (isVisible, wasVisible) => {
    if (typeof window === "undefined" || typeof document === "undefined") return;
    if (isVisible === wasVisible) return;
    if (isVisible) {
      const active = document.activeElement as HTMLElement | null;
      if (!active || active === document.body || !dialogEl.value?.contains(active)) {
        lastFocused = active && active !== document.body ? active : lastFocused;
      }
      bindKeyboard(true);
      keyboardBound = true;
      await nextTick();
      if (!visible.value) return;
      firstAction.value?.focus();
      return;
    }
    if (keyboardBound) {
      bindKeyboard(false);
      keyboardBound = false;
    }
    // 让位/挂起：焦点交给当前活动的那一层，不在这里还给页面（恢复可见时再回到框内）
  },
  { immediate: true },
);

/** 窗口真正结束（确认 / 取消 / 撤回处理后）：焦点还给打开前的元素。 */
watch(open, (isOpen, wasOpen) => {
  if (isOpen || !wasOpen) return;
  if (typeof document === "undefined") return;
  if (keyboardBound) {
    bindKeyboard(false);
    keyboardBound = false;
  }
  const target = lastFocused;
  lastFocused = null;
  if (target && document.contains(target) && typeof target.focus === "function") target.focus();
});

// 模式切换（影响确认 → 剩余撤回，或反过来）时清掉上一模式的局部说明；真实结果保留
watch(mode, () => {
  notice.value = null;
  error.value = null;
  revertError.value = null;
});

onBeforeUnmount(() => {
  if (keyboardBound) bindKeyboard(false);
  keyboardBound = false;
});
</script>

<template>
  <!-- 让位说明：更高优先级窗口开着时不叠第二个阻塞面，但明确告诉用户这边还在等着 -->
  <p v-if="deferred" class="ic-deferred" role="status" data-im="impact-confirm-deferred">
    {{ deferredText }}
  </p>

  <!-- 外部点击不关闭：影响确认只能由两个明确按钮作答（scrim 只负责遮罩，没有 click 处理）。
       v-show：让位 / 挂起时只隐藏，DOM、滚动位置与未提交的选择都不丢。
       视觉：中央悬浮 + 轻遮罩 + 轻模糊；标题左右等宽槽位、按钮组居中同高。 -->
  <Transition name="qio-fade">
    <div v-show="visible" class="ic-scrim" data-im="impact-confirm-scrim">
      <section
        ref="dialogEl"
        class="ic-dialog qio-floating qio-pop-in"
        data-im="impact-confirm"
        :data-impact-mode="mode"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="im-impact-confirm-title"
        aria-describedby="im-impact-confirm-body"
        tabindex="-1"
        @keydown.stop
        @keyup.stop
        @wheel.stop
      >
        <!-- 三列等宽：左槽只占位（不放可聚焦控件），标题严格落在中轴；右槽留空以保持对称 -->
        <div class="qio-modal-head">
          <span class="qio-modal-head__slot" aria-hidden="true" />
          <h2 id="im-impact-confirm-title" class="ic-title qio-modal-title">{{ title }}</h2>
          <span class="qio-modal-head__slot qio-modal-head__slot--end" aria-hidden="true" />
        </div>

      <div id="im-impact-confirm-body" class="ic-body" data-im="impact-confirm-body">
        <template v-if="mode === 'impact'">
          <p class="ic-lead" data-im="impact-confirm-waiting">
            <strong>等待确认，尚未保存。</strong>
            下面这些任务正在执行，而这次改动动了它们依赖的材料。选择「确认改动」才会保存并让相关任务暂停
            （进度保留）；选择「取消本次改动」则这次改动不生效，任务继续。
            其他独立任务不受影响；这不是对任务的重新审批。
          </p>
          <p v-if="note" class="ic-note" data-im="impact-confirm-note">{{ note }}</p>

          <ul class="ic-list">
            <li
              v-for="item in affected"
              :key="item.intentId"
              class="ic-item"
              data-im="impact-confirm-item"
              :data-intent-id="item.intentId"
            >
              <p class="ic-task">任务：{{ item.title || item.intentId }}</p>
              <p v-if="item.materials.length" class="ic-materials">受影响材料：{{ item.materials.join("、") }}</p>
              <p class="ic-consequence">后果：{{ item.consequence || "服务端没有给出后果说明" }}</p>
            </li>
          </ul>
          <p v-if="!affected.length" class="ic-note" data-im="impact-confirm-empty">
            服务端没有列出具体任务明细，但这次保存仍需要你确认影响范围。
          </p>
        </template>

        <template v-else>
          <p class="ic-lead" data-im="impact-revert-waiting">
            这些任务失败或取消后，已经撤回了不受影响的部分；下面这些改动会影响其他工作，所以停在这里等你决定。
          </p>
          <p class="ic-lead ic-lead-sub">
            「继续」会按上面的说明处理这个任务的剩余改动（服务端会先按当前内容重新核对）；
            「取消」保持现状、不做任何改动。多个任务各自独立，不会顺带处理你没有看到的项。
          </p>
          <p v-if="!pendingReverts.length" class="ic-note" data-im="impact-revert-empty">
            当前没有需要你决定的剩余改动。
          </p>
          <ul v-else class="ic-list">
            <li
              v-for="intent in pendingReverts"
              :key="intent.id"
              class="ic-item ic-item-revert"
              data-im="impact-revert-item"
              :data-intent-id="intent.id"
            >
              <p class="ic-task">任务：{{ intent.title || intent.id }}</p>
              <p class="ic-consequence">原因：{{ intent.revert?.reasonText || "服务端未说明" }}</p>
              <ul class="ic-pending">
                <li
                  v-for="item in intent.revert?.pendingDecision ?? []"
                  :key="item.id"
                  class="ic-pending-item"
                  data-im="impact-revert-pending-item"
                  :data-decision-id="item.id"
                >
                  <span class="ic-pending-reason">{{ item.reason }}</span>
                  <span class="ic-impact">影响：{{ item.impact }}</span>
                </li>
              </ul>
              <div class="ic-item-actions">
                <button
                  class="qio-btn primary"
                  type="button"
                  data-im="impact-revert-continue"
                  :data-intent-id="intent.id"
                  :disabled="revertStateFor(intent.id).status === 'pending'"
                  @click="continueRevert(intent.id)"
                >
                  {{ revertStateFor(intent.id).status === "pending" ? "正在处理…" : "继续：处理这个任务剩下的改动" }}
                </button>
              </div>
              <p
                v-if="revertError || revertStateFor(intent.id).status === 'error'"
                class="ic-error"
                role="alert"
                data-im="impact-revert-item-error"
              >
                这次处理没有完成：{{ revertStateFor(intent.id).error || revertError }}
              </p>
            </li>
          </ul>
        </template>
      </div>

      <!-- 部分完成 / 剩余项说明：框还开着时它不会被结果面板遮住 -->
      <p v-if="notice" class="ic-notice" role="status" data-im="impact-revert-notice">{{ notice }}</p>
      <!--
        影响确认 → 剩余撤回切换时，仍要让用户看到上一件事的真实结果。
        撤回自己的「部分完成」结果已由上面的 notice 说明（服务端逐项摘要），
        这里不再重复一条同义句（F05：同一信息不要重复出现）。
      -->
      <p
        v-if="open && outcome && mode === 'revert' && outcome.kind !== 'revert_partial' && outcome.kind !== 'revert_done'"
        class="ic-notice"
        role="status"
        data-im="impact-confirm-outcome-inline"
      >
        {{ outcome.title }}：{{ outcome.text }}
      </p>
      <p v-if="error" class="ic-error" role="alert" data-im="impact-confirm-error">操作失败：{{ error }}</p>
      <p v-if="revertError" class="ic-error" role="alert" data-im="impact-revert-error">
        这次处理没有完成：{{ revertError }}
      </p>

        <!-- 按钮组整体居中、同高、配对同宽；窄窗（<=480）自动纵排 -->
        <div class="ic-actions qio-modal-actions">
          <button
            ref="firstAction"
            class="qio-btn"
            type="button"
            data-im="impact-confirm-cancel"
            :disabled="busy"
            @click="mode === 'impact' ? cancel() : dismissReverts()"
          >
            {{
              mode === "impact"
                ? busy
                  ? "处理中…"
                  : "取消本次改动（不保存，任务继续）"
                : "取消：保持现状，不做改动"
            }}
          </button>
          <button
            v-if="mode === 'impact'"
            class="qio-btn primary"
            type="button"
            data-im="impact-confirm-submit"
            :disabled="busy"
            @click="confirm"
          >
            {{ busy ? "处理中…" : "确认改动（保存并暂停相关任务）" }}
          </button>
        </div>
      </section>
    </div>
  </Transition>

  <!-- 真实结果：活过对话框关闭，按 store 的真实结果说话 -->
  <section
    v-if="!open && !deferred && outcome"
    class="ic-outcome qio-floating"
    role="status"
    aria-live="polite"
    data-im="impact-confirm-outcome"
    :data-outcome="outcome.kind"
  >
    <h2 class="ic-outcome-title">{{ outcome.title }}</h2>
    <p class="ic-outcome-text">{{ outcome.text }}</p>
    <div v-if="outcome.kind === 'saved'" class="ic-paused" data-im="impact-confirm-paused">
      <p>{{ outcome.paused.length ? "这次被暂停的任务：" : "这次没有任务因为这项改动被暂停。" }}</p>
      <ul v-if="outcome.paused.length">
        <li v-for="intent in outcome.paused" :key="intent.id">{{ intent.title }}</li>
      </ul>
    </div>
    <div class="ic-actions qio-modal-actions">
      <button
        v-if="outcome.kind === 'check_failed' || outcome.kind === 'save_failed' || outcome.kind === 'superseded'"
        class="qio-btn primary"
        type="button"
        data-im="impact-confirm-retry"
        :disabled="retryBusy"
        @click="retrySave"
      >{{ retryBusy ? "核实中…" : "重新核实并保存" }}</button>
      <button
        v-if="outcome.kind === 'cancelled_unconfirmed'"
        class="qio-btn primary"
        type="button"
        data-im="impact-confirm-recovery-retry"
        :disabled="retryBusy"
        @click="retryCancelRecovery"
      >{{ retryBusy ? "核对中…" : "重新核对服务器事实" }}</button>
      <button class="qio-btn" type="button" data-im="impact-confirm-outcome-close" @click="dismissOutcome">
        知道了
      </button>
    </div>
  </section>

  <!-- 被结束提示、但服务端确实还有待决定项：不自动打断，只给一个可控的「再次查看」入口 -->
  <section
    v-if="!deferred && dismissedReverts.length"
    class="ic-dismissed qio-floating"
    role="status"
    aria-label="还有撤回没有决定"
    data-im="impact-revert-dismissed"
  >
    <h2 class="ic-outcome-title">还有撤回没有决定</h2>
    <p class="ic-outcome-text">这些任务的剩余改动按你的要求暂时不再提示；需要处理时再打开。</p>
    <ul class="ic-outcome-list">
      <li v-for="intent in dismissedReverts" :key="intent.id">
        <span>{{ intent.title || intent.id }}</span>
        <button
          class="qio-btn quiet"
          type="button"
          data-im="impact-revert-reopen"
          :data-intent-id="intent.id"
          @click="reopenRevert(intent.id)"
        >再次查看</button>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.ic-deferred {
  position: absolute;
  left: 50%;
  bottom: 180px;
  transform: translateX(-50%);
  z-index: calc(var(--im-z-dialog) + 1);
  max-width: min(560px, calc(100vw - 32px));
  margin: 0;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--warning);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated);
  color: var(--warning);
  font-size: var(--fs-xs);
  line-height: 1.6;
  text-align: center;
  overflow-wrap: anywhere;
}
.ic-scrim {
  position: fixed;
  inset: 0;
  z-index: calc(var(--im-z-dialog) + 30);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-6);
  /* 轻遮罩 + 轻模糊：仍能辨认原页面，不做成「另一个应用的黑屏」 */
  background: color-mix(in srgb, var(--bg-base) 52%, transparent);
  backdrop-filter: blur(calc(var(--glass-blur) * 0.4));
  -webkit-backdrop-filter: blur(calc(var(--glass-blur) * 0.4));
}
.ic-dialog {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  width: min(560px, 100%);
  /* 对话框本身不滚（clip 不建立滚动容器）：滚动收敛到正文一层（.ic-body），
     标题 / 说明 / 操作区固定在框内，再长的影响清单也藏不掉「取消 / 确认」。 */
  max-height: min(84vh, 760px);
  overflow: clip;
  padding: var(--sp-4);
  color: var(--text-primary);
  font-family: var(--sans);
}
.ic-dialog:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.ic-title { font-family: var(--serif); font-size: var(--fs-lg); color: var(--text-strong); }
/* 正文是这一框里唯一的主要滚动层（长清单在这里滚，操作区不动） */
.ic-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  flex: 1 1 auto;
  min-height: 0;
  min-width: 0;
  overflow-y: auto;
  overflow-x: clip;
  overscroll-behavior: contain;
  /* 标题 / 说明 / 状态居中是本轮的视觉要求；明细清单（任务、材料）保持左对齐便于扫读 */
  text-align: center;
}
.ic-lead, .ic-note, .ic-task, .ic-materials, .ic-consequence { margin: 0; font-size: var(--fs-sm); line-height: 1.65; overflow-wrap: anywhere; }
.ic-lead { color: var(--text-primary); text-align: center; }
.ic-lead-sub { color: var(--text-muted); }
.ic-note { color: var(--warning); font-size: var(--fs-xs); text-align: center; }
.ic-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
.ic-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
  text-align: left;
}
.ic-item-revert { border-left: 2px solid var(--warning); }
.ic-task { color: var(--text-strong); }
.ic-materials { color: var(--text-secondary); font-family: var(--mono); font-size: var(--fs-xs); }
.ic-consequence { color: var(--text-secondary); font-size: var(--fs-xs); }
.ic-pending { list-style: none; margin: var(--sp-1) 0 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.ic-pending-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-left: var(--sp-3);
  border-left: 2px solid var(--warning);
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.ic-pending-reason { color: var(--text-primary); }
.ic-impact { color: var(--warning); }
.ic-item-actions { display: flex; gap: var(--sp-2); flex-wrap: wrap; margin-top: var(--sp-1); }
.ic-notice { margin: 0; color: var(--text-secondary); font-size: var(--fs-xs); line-height: 1.6; overflow-wrap: anywhere; }
.ic-error { margin: 0; color: var(--danger); font-size: var(--fs-xs); overflow-wrap: anywhere; }
/* 动作区统一由 .qio-modal-actions 提供：居中、等宽、同高、窄窗纵排 */
.ic-actions { flex: none; }
.ic-outcome {
  position: absolute;
  left: 50%;
  bottom: 140px;
  transform: translateX(-50%);
  z-index: calc(var(--im-z-dialog) + 31);
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  width: min(560px, calc(100vw - 32px));
  padding: var(--sp-3);
  color: var(--text-primary);
  font-family: var(--sans);
  text-align: center;
}
.ic-outcome-title { margin: 0; font-family: var(--serif); font-size: var(--fs-sm); color: var(--text-strong); text-align: center; }
.ic-outcome-text, .ic-paused p { margin: 0; font-size: var(--fs-xs); line-height: 1.65; color: var(--text-secondary); overflow-wrap: anywhere; text-align: center; }
.ic-paused ul, .ic-outcome-list { margin: 0; padding-left: 1.2em; font-size: var(--fs-xs); color: var(--text-secondary); }
/* 被结束提示但仍有待决定项：右下角一条可操作的提醒，不抢中央阻塞面 */
.ic-dismissed {
  position: absolute;
  right: var(--sp-3);
  bottom: 120px;
  z-index: calc(var(--im-z-dialog) + 2);
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  width: min(360px, calc(100vw - 32px));
  padding: var(--sp-2) var(--sp-3);
  color: var(--text-primary);
  font-family: var(--sans);
  text-align: center;
}
.ic-outcome-list { padding-left: 0; list-style: none; display: flex; flex-direction: column; gap: var(--sp-1); }
.ic-outcome-list li { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2); flex-wrap: wrap; }
@media (max-width: 700px) {
  .ic-outcome { bottom: 8px; width: calc(100% - 16px); }
  .ic-deferred { bottom: 150px; }
  .ic-dismissed { bottom: 96px; right: 8px; }
}
</style>
