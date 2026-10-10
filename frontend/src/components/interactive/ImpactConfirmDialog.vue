<!--
  页面中央的影响确认框（结构由 D 建立；本轮「显示与入口」由 C 调整：文案层级、辅助说明、
  不出现开发术语；不改任何 store 行为）。

  契约 §1.6 / §8.1：
  - 用户要改**执行中任务**依赖的材料时，在**改动生效前**先用页面中央的确认框说明
    受影响的对象、任务与后果；不用顶部横幅承担这一步；
  - 确认 = 改动生效 + 相关任务暂停并保留进度；取消 = 改动不生效、任务继续；
    其他独立任务不受影响；
  - 不先改数据再询问：板面还没保存（store.saveNow 在预判命中时直接返回，pendingImpact 只是待决定）；
  - 这不是新的任务审批：已经批准过的任务不会被重新审批，只会被暂停并保留进度；
  - 键盘：打开时聚焦框内第一个按钮，Tab / Shift+Tab 限制在框内，Escape = 取消，
    关闭后焦点回到触发点；
  - 失败 / 取消后的部分撤回若还有需要决定的部分，用同一个框说明剩余改动与具体影响。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import type { ImpactConfirmOutcome, Intent } from "../../interactive/types";

const store = useInteractiveStore();

const dialog = ref<HTMLElement | null>(null);
const firstAction = ref<HTMLButtonElement | null>(null);
const busy = ref(false);
/**
 * 这次提示自己的说明（只在框还开着时有意义）。
 *
 * N3 之后「取消」不再把整条提示说成已结束 —— 提示是按**任务身份**结束的，
 * 还有别的任务有待决定项时框会继续开着。
 */
const notice = ref<string | null>(null);
const error = ref<string | null>(null);
/** 撤回决定的最新真实失败原因（来自 store 的 revertDecisionStateFor） */
const revertError = ref<string | null>(null);

/**
 * 最近一次「真实操作结果」（N2）：确认 / 取消都会产生一条。
 * 它必须能活过对话框关闭 —— 确认后 pendingImpact 被清空、框会收起，
 * 但用户仍然需要看到真实结果；所以这块单独渲染、不依赖 open。
 */
const outcome = ref<{ kind: ImpactConfirmOutcome | "cancelled_unconfirmed"; text: string; paused: Intent[] } | null>(null);
/** 撤回还没被服务器事实确认时的重试进行态 */
const recoveryBusy = ref(false);

/** 打开前焦点在哪：关闭后要还回去 */
let lastFocused: HTMLElement | null = null;

const affected = computed(() => store.pendingImpact?.affected ?? []);

/**
 * 服务端/主流程补充的说明（可选字段 note，可能为 undefined）。
 * 它是对「为什么又让你确认一次」的**辅助说明**，层级低于主决定：
 * 主决定永远是「确认 / 取消」两个按钮，note 只解释情况，不改变按钮含义。
 */
const impactNote = computed(() => String((store.pendingImpact as unknown as { note?: string } | null)?.note ?? "").trim());

/**
 * 撤回留下的「等待你决定」：同一批（正在执行 / 已暂停）任务失败或取消后，
 * 若还有需要决定的部分，用同一个框说明剩余改动与具体影响。
 *
 * N3：被用户主动结束提示的任务（dismissed）不再出现在这里 —— 不因刷新或任务列表更新
 * 无理由反复打断；用户可以在同一页的「还有撤回没有决定」入口主动再次查看。
 */
const pendingReverts = computed(() =>
  store.intents.filter(
    (intent) =>
      (intent.revert?.pendingDecision?.length ?? 0) > 0 &&
      ["running", "paused", "failed", "cancelled"].includes(intent.status) &&
      !store.revertDecisionDismissed?.[intent.id],
  ),
);

/** 被结束提示、但服务端确实还有待决定项的任务身份（用于「再次查看」入口） */
const dismissedReverts = computed(() =>
  store.intents.filter(
    (intent) =>
      (intent.revert?.pendingDecision?.length ?? 0) > 0 &&
      ["running", "paused", "failed", "cancelled"].includes(intent.status) &&
      Boolean(store.revertDecisionDismissed?.[intent.id]),
  ),
);

const open = computed(() => Boolean(store.pendingImpact) || pendingReverts.value.length > 0);
const mode = computed<"impact" | "revert">(() => (store.pendingImpact ? "impact" : "revert"));

const title = computed(() =>
  mode.value === "impact"
    ? "这次改动还没有生效：它会影响正在执行的任务"
    : "还有改动没有撤回：需要你决定剩下的部分",
);

/** 这次真正展示给用户在撤回模式下处理的决定项（按任务身份，只取当前展示的那些） */
function shownDecisionIds(intent: Intent): string[] {
  return (intent.revert?.pendingDecision ?? []).map((item) => item.id);
}

/** 这个任务这次展示的决定项还要不要处理（成功返回后可能已经没有了） */
function currentDecisionIds(intentId: string): string[] {
  const fresh = store.intents.find((item) => item.id === intentId) ?? null;
  return fresh ? shownDecisionIds(fresh) : [];
}

function revertStateFor(intentId: string): { status: "idle" | "pending" | "error"; error: string | null } {
  return store.revertDecisionStateFor(intentId);
}

function recallFocus(): void {
  const target = lastFocused;
  lastFocused = null;
  if (target && typeof target.focus === "function" && document.contains(target)) {
    target.focus();
  }
}

function onKeydown(event: KeyboardEvent): void {
  if (!open.value) return;
  if (event.key === "Escape") {
    event.preventDefault();
    if (mode.value === "impact") void cancel();
    else dismiss();
    return;
  }
  if (event.key !== "Tab") return;
  const root = dialog.value;
  if (!root) return;
  const focusables = [...root.querySelectorAll<HTMLElement>("button:not([disabled])")];
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const active = document.activeElement as HTMLElement | null;
  // 焦点限制在框内：到边界就回卷，绝不跑到页面其它地方
  if (event.shiftKey && (active === first || !root.contains(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (active === last || !root.contains(active))) {
    event.preventDefault();
    first.focus();
  }
}

watch(
  open,
  async (isOpen, wasOpen) => {
    if (typeof document === "undefined") return;
    if (isOpen && !wasOpen) {
      const active = document.activeElement as HTMLElement | null;
      lastFocused = active && active !== document.body ? active : null;
      notice.value = null;
      error.value = null;
      revertError.value = null;
      await nextTick();
      firstAction.value?.focus();
    } else if (!isOpen && wasOpen) {
      recallFocus();
    }
  },
  { immediate: true },
);

watch(mode, () => {
  notice.value = null;
  error.value = null;
  revertError.value = null;
});

if (typeof window !== "undefined") {
  window.addEventListener("keydown", onKeydown, true);
}
onBeforeUnmount(() => {
  if (typeof window !== "undefined") window.removeEventListener("keydown", onKeydown, true);
});

/**
 * 确认：改动生效，相关任务暂停并保留进度（不是重新审批任务）。
 *
 * N2：只依据 store 返回的**真实结果**说话。Promise 正常返回不等于操作成功：
 * 仍待确认 / 检查失败 / 保存失败 / 板面又变了都要如实说，绝不提前宣称「已保存、已暂停」。
 */
async function confirm(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  error.value = null;
  try {
    const result = await store.confirmImpact();
    if (store.pendingImpact) {
      // 还需要再次确认：这次没有保存，任务也没有被暂停
      outcome.value = {
        kind: "needs_confirm",
        text: result?.reason || "这次改动还没有保存，正在等你确认影响范围",
        paused: [],
      };
      return;
    }
    const kind: ImpactConfirmOutcome = result?.outcome ?? "superseded";
    if (kind === "saved") {
      outcome.value = {
        kind: "saved",
        text: "改动已保存生效；受影响的任务已暂停并保留进度（不会自动继续，需要你在任务浮层里确认后按当前材料继续）。",
        paused: result?.paused ?? [],
      };
      return;
    }
    outcome.value = {
      kind,
      text: result?.reason || "这次改动没有落库，也没有任务被暂停",
      paused: [],
    };
  } catch (err) {
    const message = (err as Error).message || "确认没有完成";
    error.value = message;
    outcome.value = { kind: "save_failed", text: message, paused: [] };
  } finally {
    busy.value = false;
  }
}

/**
 * 取消：改动不生效，任务继续（板面回到已保存状态）。
 * F1：撤回是否真的被服务器事实确认看 store.cancelRecovery —— 没确认就不许说「已撤回」。
 */
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
        text: recovery.reason || "撤回还没有被服务器事实确认",
        paused: [],
      };
      return;
    }
    outcome.value = {
      kind: "cancelled",
      text: "已取消：这次改动没有生效，任务继续；其他独立任务不受影响。",
      paused: [],
    };
  } catch (err) {
    const message = (err as Error).message || "取消失败";
    error.value = message;
    outcome.value = { kind: "cancelled_unconfirmed", text: message, paused: [] };
  } finally {
    busy.value = false;
  }
}

/** 真实结果面板的标题：说清楚「到底发生了什么」，不用开发术语 */
const outcomeTitle = computed(() => {
  switch (outcome.value?.kind) {
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
    default:
      return "撤回还没有被服务器事实确认";
  }
});

/** 结束这条结果说明（真实结果已经看过；板面不受影响） */
function dismissOutcome(): void {
  outcome.value = null;
  recoveryBusy.value = false;
}

/** 检查失败 / 保存失败之后的可用重试入口：重新做影响预判并保存 */
async function retrySave(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  try {
    await store.saveNow();
  } catch (err) {
    error.value = (err as Error).message;
  } finally {
    busy.value = false;
    if (store.impactCheckError) {
      outcome.value = { kind: "check_failed", text: store.impactCheckError, paused: [] };
    } else if (store.saveStatus === "error") {
      outcome.value = { kind: "save_failed", text: store.saveError || "保存没有完成", paused: [] };
    } else if (store.saveStatus === "saved") {
      outcome.value = { kind: "saved", text: "改动已保存生效。", paused: [...store.lastSavePaused] };
    }
  }
}

/** 撤回还没被服务器确认时的重试：重新核对服务器事实 */
async function retryCancelRecovery(): Promise<void> {
  if (recoveryBusy.value) return;
  recoveryBusy.value = true;
  try {
    const result = await store.retryCancelRecovery();
    if (result.ok) {
      outcome.value = { kind: "cancelled", text: "已取消：这次改动没有生效，任务继续。", paused: [] };
    } else {
      outcome.value = {
        kind: "cancelled_unconfirmed",
        text: result.error || store.cancelRecovery?.reason || "撤回还没有被服务器事实确认",
        paused: [],
      };
    }
  } finally {
    recoveryBusy.value = false;
  }
}

/**
 * N3「继续」：**只**处理这次明确展示给用户的剩余决定项。
 * 真的执行（带 decisionIds，服务端 N4 会按当前内容重核），拿到真实结果再说话：
 * 失败 → 显示服务端真实原因 + 可重试；成功但仍有剩余 → 提示还有多少项没有处理。
 */
async function continueRevert(intentId: string): Promise<void> {
  if (!intentId) return;
  if (revertStateFor(intentId).status === "pending") return;
  error.value = null;
  revertError.value = null;
  const decisionIds = currentDecisionIds(intentId);
  try {
    const result = await store.continueRevertDecision(intentId, decisionIds);
    if (result?.ok) {
      const remaining = currentDecisionIds(intentId);
      if (remaining.length) {
        notice.value = "已处理这次展示的 " + decisionIds.length + " 项；这个任务还有 " + remaining.length + " 项需要你决定。";
      } else {
        notice.value = "已按上面的说明处理完这个任务剩下的改动。";
      }
      return;
    }
    revertError.value = result?.reason || result?.detail || "这次撤回没有执行";
  } catch (err) {
    revertError.value = (err as Error).message || "这次撤回没有执行";
  }
}

/**
 * N3「取消」/ Escape：只结束**本次提示**并保留板面，不执行任何撤回。
 * 结束是按任务身份记的（store.revertDecisionDismissed），同一会话内任务列表更新不会反复打断。
 */
function dismiss(): void {
  const targets = pendingReverts.value.map((intent) => intent.id);
  for (const id of targets) store.dismissRevertDecision(id);
  notice.value = null;
  error.value = null;
  revertError.value = null;
}

/** 用户主动再次查看某个任务的待决定项 */
function reopen(intentId: string): void {
  store.reopenRevertDecision(intentId);
}
</script>

<template>
  <div v-if="open" class="backdrop qio-confirm-scrim" data-im="impact-scrim" data-im-scrim="impact">
    <div
      ref="dialog"
      class="dialog qio-confirm qio-confirm--layer"
      data-im="impact-dialog"
      :data-impact-mode="mode"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="im-impact-title"
      aria-describedby="im-impact-body"
      tabindex="-1"
      @keydown.stop
      @keyup.stop
      @wheel.stop
    >
      <h2 id="im-impact-title" class="title">{{ title }}</h2>

      <div id="im-impact-body" class="body">
        <template v-if="mode === 'impact'">
          <p class="lead">
            下面这些任务正在执行，而这次改动动了它们依赖的材料。<strong>改动还没有生效。</strong>
          </p>
          <p class="lead-sub">
            选择「继续」才会保存并让它们暂停（进度保留）；选择「取消」则这次改动不生效，任务继续。
            其他独立任务不受影响；这不是对任务的重新审批。
          </p>
          <p v-if="impactNote" class="note-aux" data-im="impact-note">{{ impactNote }}</p>
          <ul class="list">
            <li v-for="item in affected" :key="item.intentId" class="item" data-im="impact-item">
              <p class="task">任务：{{ item.title }}</p>
              <p v-if="item.materials.length" class="materials mono">
                受影响材料：{{ item.materials.join("、") }}
              </p>
              <p class="consequence">后果：{{ item.consequence }}</p>
            </li>
          </ul>
        </template>

        <template v-else>
          <p class="lead">
            这些任务失败或取消后，已经撤回了不受影响的部分；下面这些改动会影响其他工作，所以停在这里等你决定。
          </p>
          <p class="lead-sub">
            「继续」会按上面的说明处理这个任务的剩余改动（服务端会先按当前内容重新核对）；
            「取消」保持现状、不做任何改动。多个任务各自独立，不会顺带处理你没有看到的项。
          </p>
          <ul class="list">
            <li v-for="intent in pendingReverts" :key="intent.id" class="item" data-im="impact-item">
              <p class="task">任务：{{ intent.title }}</p>
              <p class="consequence">原因：{{ intent.revert?.reasonText || "未说明" }}</p>
              <ul class="pending">
                <li v-for="item in intent.revert?.pendingDecision ?? []" :key="item.id">
                  {{ item.reason }}
                  <span class="impact">影响：{{ item.impact }}</span>
                </li>
              </ul>
              <div class="item-actions">
                <button
                  class="btn qio-btn primary"
                  type="button"
                  data-im="impact-item-continue"
                  :data-intent-id="intent.id"
                  :disabled="revertStateFor(intent.id).status === 'pending'"
                  @click="continueRevert(intent.id)"
                >
                  {{ revertStateFor(intent.id).status === "pending" ? "正在处理…" : "继续：按上面的说明处理这个任务的剩余改动" }}
                </button>
              </div>
              <p
                v-if="revertError || revertStateFor(intent.id).status === 'error'"
                class="error"
                role="alert"
                data-im="impact-revert-error"
              >
                这次处理没有完成：{{ revertError || revertStateFor(intent.id).error }}
              </p>
            </li>
          </ul>
        </template>
      </div>

      <p v-if="notice" class="notice" role="status" data-im="impact-notice">{{ notice }}</p>
      <p v-if="error && mode === 'impact'" class="error" role="alert" data-im="impact-error">操作失败：{{ error }}</p>

      <div class="actions qio-confirm__actions">
        <button
          ref="firstAction"
          class="btn qio-btn quiet"
          type="button"
          data-im="impact-cancel"
          :disabled="busy"
          @click="mode === 'impact' ? cancel() : dismiss()"
        >
          {{ mode === "impact" ? "取消：改动不生效，任务继续" : "取消：保持现状，不做改动" }}
        </button>
        <button
          v-if="mode === 'impact'"
          class="btn qio-btn primary"
          type="button"
          data-im="impact-continue"
          :disabled="busy"
          @click="confirm()"
        >
          继续：改动生效，相关任务暂停并保留进度
        </button>
        <!--
          撤回模式：只有一个待决定任务时给一个整框级的「继续」；
          多个任务时每个任务用自己那一行的按钮 —— 不做「一次全处理」，避免顺带处理没展示的项。
        -->
        <button
          v-else-if="pendingReverts.length === 1"
          class="btn qio-btn primary"
          type="button"
          data-im="impact-continue"
          :disabled="revertStateFor(pendingReverts[0].id).status === 'pending'"
          @click="continueRevert(pendingReverts[0].id)"
        >
          {{ revertStateFor(pendingReverts[0].id).status === "pending" ? "正在处理…" : "继续：按上面的说明处理剩余改动" }}
        </button>
      </div>
    </div>
  </div>

  <Teleport to="body">
    <section
      v-if="outcome"
      class="outcome"
      data-im="impact-outcome"
      :data-outcome="outcome.kind"
      role="status"
      aria-live="polite"
      aria-label="操作结果"
    >
      <h2 class="outcome-title">{{ outcomeTitle }}</h2>
      <p class="outcome-text">{{ outcome.text }}</p>
      <div v-if="outcome.kind === 'saved'" class="outcome-paused" data-im="impact-outcome-paused">
        <p class="outcome-text">
          {{ outcome.paused.length ? "这次被暂停的任务：" : "这次没有任务因为这项改动被暂停。" }}
        </p>
        <ul v-if="outcome.paused.length" class="outcome-list">
          <li v-for="intent in outcome.paused" :key="intent.id" data-im="impact-outcome-paused-item">
            {{ intent.title }}
          </li>
        </ul>
      </div>
      <div class="outcome-actions">
        <button
          v-if="outcome.kind === 'check_failed' || outcome.kind === 'save_failed'"
          class="btn qio-btn primary"
          type="button"
          data-im="impact-retry"
          :disabled="busy"
          @click="retrySave()"
        >
          重新核实并保存
        </button>
        <button
          v-if="outcome.kind === 'cancelled_unconfirmed'"
          class="btn qio-btn primary"
          type="button"
          data-im="impact-cancel-retry"
          :disabled="recoveryBusy"
          @click="retryCancelRecovery()"
        >
          重新核对服务器事实
        </button>
        <button
          class="btn qio-btn quiet"
          type="button"
          data-im="impact-outcome-close"
          @click="dismissOutcome()"
        >
          知道了
        </button>
      </div>
    </section>
    <!--
      被结束提示、但服务端确实还有待决定项：不自动打断，只给一个可控的「再次查看」入口。
      这是「不再反复打断」与「信息仍然可达」之间的那条线。
    -->
    <section v-if="dismissedReverts.length" class="outcome dismissed" data-im="impact-dismissed" role="status" aria-label="还有撤回没有决定">
      <h2 class="outcome-title">还有撤回没有决定</h2>
      <p class="outcome-text">这些任务的剩余改动按你的要求暂时不再提示；需要处理时再打开。</p>
      <ul class="outcome-list">
        <li v-for="intent in dismissedReverts" :key="intent.id">
          {{ intent.title }}
          <button
            class="btn qio-btn quiet"
            type="button"
            data-im="impact-dismissed-reopen"
            :data-intent-id="intent.id"
            @click="reopen(intent.id)"
          >
            再次查看
          </button>
        </li>
      </ul>
    </section>
  </Teleport>
</template>

<style scoped>
/* 遮罩与层级走既有原语（.qio-confirm-scrim 用令牌 --bg-overlay，不再自带 rgba 兜底） */
.backdrop {
  z-index: var(--im-z-dialog, 60);
}
.dialog {
  width: min(560px, calc(100vw - var(--sp-6)));
  max-height: min(80vh, 640px);
  overflow: auto;
  overscroll-behavior: contain;
  padding: var(--sp-5);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  background: var(--bg-elevated);
  color: var(--text-primary);
  box-shadow: var(--elev-overlay, var(--shadow-3));
}
.dialog:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.title {
  margin: 0 0 var(--sp-3);
  font-family: var(--serif);
  font-size: var(--fs-md);
  color: var(--text-strong);
}
.body { display: flex; flex-direction: column; gap: var(--sp-2); }
/* 说明文字是用户做决定要读的东西：用正文级字号，不缩成元信息级 */
.lead {
  margin: 0;
  font-size: var(--fs-base);
  line-height: var(--lh-base);
  color: var(--text-secondary);
}
/* 辅助说明层：比主句低一档（字号小一档、颜色更安静），用户可以先读主句做决定 */
.lead-sub {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--text-muted);
}
/* 需要重新核对时的补充说明：同一层级里的「注意」，用左边线而不是整块变色，避免抢走主决定 */
.note-aux {
  margin: 0;
  padding: var(--sp-1) var(--sp-3);
  border-left: 2px solid var(--warning);
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--text-secondary);
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.item {
  border-left: 2px solid var(--danger);
  padding-left: var(--sp-3);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.task { margin: 0; font-size: var(--fs-base); color: var(--text-strong); }
.materials { margin: 0; font-size: var(--fs-sm); color: var(--text-secondary); }
.consequence {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--text-secondary);
}
.pending {
  margin: 0;
  padding-left: var(--sp-4);
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  line-height: var(--lh-base);
}
.impact { color: var(--warning); }
.notice,
.error {
  margin: var(--sp-3) 0 0;
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
}
.notice { color: var(--text-secondary); }
.error { color: var(--danger); }
.actions {
  margin-top: var(--sp-4);
  display: flex;
  gap: var(--sp-3);
  flex-wrap: wrap;
  justify-content: flex-end;
}
/*
  按钮复用全局原语 .qio-btn（圆角 / 边框 / 语义色 / 悬停 / 禁用都来自同一套语言），
  这里只把高度放开：两个按钮的文案是一句完整的话，写死 34px 会把第二行裁掉。
*/
.btn {
  height: auto;
  min-height: 34px;
  padding: var(--sp-2) var(--sp-4);
  line-height: var(--lh-tight);
  text-align: left;
}
.btn.quiet { border-color: transparent; background: none; color: var(--text-secondary); }
.btn.quiet:hover:enabled { background: var(--layer-hover); color: var(--text-strong); }
.btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
/* 待决定项自己的操作：层级低于整框的「取消」，但必须真的可点、可重试 */
.item-actions { display: flex; gap: var(--sp-2); flex-wrap: wrap; margin-top: var(--sp-1); }
/* 广泛的失败原因不能截断：长中文 / 长原文允许换行，必要时局部滚动 */
.item .error { overflow-wrap: anywhere; }
/* 真实结果面板：确认之后对话框会收起，结果必须还能看见（宽窗口右下、窄窗口居中贴底） */
.outcome {
  position: fixed;
  right: var(--sp-4);
  bottom: var(--sp-4);
  z-index: var(--im-z-result, 62);
  width: min(420px, calc(100vw - var(--sp-6)));
  max-height: min(60vh, 420px);
  overflow: auto;
  padding: var(--sp-3) var(--sp-4);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  box-shadow: var(--elev-overlay, var(--shadow-3));
  color: var(--text-primary);
}
.outcome-title {
  margin: 0 0 var(--sp-1);
  font-family: var(--serif);
  font-size: var(--fs-sm);
  color: var(--text-strong);
}
.outcome-text {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-base);
  line-height: var(--lh-base);
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.outcome-paused { margin-bottom: var(--sp-2); }
.outcome-list {
  margin: 0;
  padding-left: var(--sp-4);
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}
.outcome-actions { display: flex; gap: var(--sp-2); flex-wrap: wrap; }
.outcome .btn { min-height: 30px; height: auto; padding: var(--sp-1) var(--sp-3); line-height: var(--lh-tight); }
@media (max-width: 560px) {
  .outcome { right: var(--sp-2); bottom: var(--sp-2); left: var(--sp-2); width: auto; max-height: 50vh; }
}
</style>
