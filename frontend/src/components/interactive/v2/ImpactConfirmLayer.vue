<!--
  F01：保存前的影响确认（A 组）。

  基线缺陷：InteractiveView 只挂了统一审批层，store.saveNow 产生的 pendingImpact
  （受影响任务 / 材料 / 后果 + checkId）**没有任何界面承接**：用户看到「有改动尚未保存」，
  却永远没有确认入口（死等待）。旧的 ImpactConfirmDialog 已实现但没有挂载。

  这个组件是互动页上**唯一**的影响确认面：
  - 明确展示真实受影响任务、材料与操作后果，并写明「等待确认，尚未保存」；
  - 两个决定入口：确认改动（保存并暂停相关任务）/ 取消本次改动（不保存、任务继续）；
  - 结果是 store 的真实结果（保存回执 / 预判失败 / 保存失败 / 又被更新的改动接手），
    绝不在 Promise 返回时就宣称成功；
  - 与工具/权限审批共用「同时只有一个阻塞确认」的规则：审批弹窗打开时让位，
    关闭后影响确认自动回来（不会两个阻塞弹窗叠在一起，也不会永远解不除）。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { useUnifiedApprovalsStore } from "../../../stores/unifiedApprovals";
import type { ImpactConfirmOutcome, Intent } from "../../../interactive/types";

const store = useInteractiveStore();
const unified = useUnifiedApprovalsStore();

const open = computed(() => Boolean(store.pendingImpact));
/** 统一审批弹窗开着时让位：同一时刻只允许一个阻塞确认面 */
const deferred = computed(() => open.value && unified.visible);
const affected = computed(() => store.pendingImpact?.affected ?? []);
const note = computed(() => String(store.pendingImpact?.note ?? "").trim());

const busy = ref(false);
const error = ref<string | null>(null);
/**
 * 最近一次**真实结果**：必须活过对话框关闭（确认后 pendingImpact 被清空、框会收起，
 * 但用户仍然需要看到保存回执 / 失败原因）。
 */
const outcome = ref<{
  kind: ImpactConfirmOutcome | "cancelled_unconfirmed" | "cancelled";
  title: string;
  text: string;
  paused: Intent[];
} | null>(null);
const retryBusy = ref(false);

function outcomeTitle(kind: string): string {
  switch (kind) {
    case "saved":
      return "改动已保存生效";
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
}

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

/** Escape = 取消本次改动（与按钮同一语义；不会自动确认）。 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key !== "Escape" || !open.value || busy.value) return;
  event.preventDefault();
  void cancel();
}

watch(
  open,
  (isOpen, wasOpen) => {
    if (typeof window === "undefined") return;
    if (isOpen && !wasOpen) {
      window.addEventListener("keydown", onKeydown, true);
      error.value = null;
    } else if (!isOpen && wasOpen) {
      window.removeEventListener("keydown", onKeydown, true);
    }
  },
  { immediate: true },
);

onBeforeUnmount(() => {
  if (typeof window !== "undefined") window.removeEventListener("keydown", onKeydown, true);
});
</script>

<template>
  <!-- 让位说明：审批弹窗打开时不叠第二个阻塞面，但明确告诉用户影响确认还在等着 -->
  <p v-if="deferred" class="ic-deferred" role="status" data-im="impact-confirm-deferred">
    影响确认还在等你决定（这次改动尚未保存）。先处理完工具 / 权限审批，随后会自动回到影响确认。
  </p>

  <div v-if="open && !deferred" class="ic-scrim" data-im="impact-confirm-scrim">
    <section
      class="ic-dialog"
      data-im="impact-confirm"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="im-impact-confirm-title"
    >
      <h2 id="im-impact-confirm-title" class="ic-title">这次改动还没有生效：它会影响正在执行的任务</h2>
      <p class="ic-lead" data-im="impact-confirm-waiting">
        <strong>等待确认，尚未保存。</strong>
        选择「确认改动」才会保存并让相关任务暂停（进度保留）；选择「取消本次改动」则这次改动不生效，任务继续。
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

      <p v-if="error" class="ic-error" role="alert" data-im="impact-confirm-error">{{ error }}</p>

      <div class="ic-actions">
        <button
          class="ic-btn"
          type="button"
          data-im="impact-confirm-cancel"
          :disabled="busy"
          @click="cancel"
        >{{ busy ? "处理中…" : "取消本次改动（不保存，任务继续）" }}</button>
        <button
          class="ic-btn primary"
          type="button"
          data-im="impact-confirm-submit"
          :disabled="busy"
          @click="confirm"
        >{{ busy ? "处理中…" : "确认改动（保存并暂停相关任务）" }}</button>
      </div>
    </section>
  </div>

  <!-- 真实结果：活过对话框关闭，按 store 的真实结果说话 -->
  <section
    v-if="!open && outcome"
    class="ic-outcome"
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
    <div class="ic-actions">
      <button
        v-if="outcome.kind === 'check_failed' || outcome.kind === 'save_failed' || outcome.kind === 'superseded'"
        class="ic-btn primary"
        type="button"
        data-im="impact-confirm-retry"
        :disabled="retryBusy"
        @click="retrySave"
      >{{ retryBusy ? "核实中…" : "重新核实并保存" }}</button>
      <button
        v-if="outcome.kind === 'cancelled_unconfirmed'"
        class="ic-btn primary"
        type="button"
        data-im="impact-confirm-recovery-retry"
        :disabled="retryBusy"
        @click="retryCancelRecovery"
      >{{ retryBusy ? "核对中…" : "重新核对服务器事实" }}</button>
      <button class="ic-btn" type="button" data-im="impact-confirm-outcome-close" @click="dismissOutcome">
        知道了
      </button>
    </div>
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
  border-radius: var(--r-lg);
  background: var(--bg-elevated);
  color: var(--warning);
  font-size: var(--fs-xs);
  line-height: 1.6;
}
.ic-scrim {
  position: fixed;
  inset: 0;
  z-index: calc(var(--im-z-dialog) + 30);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-4);
  background: color-mix(in srgb, var(--bg-base) 62%, transparent);
}
.ic-dialog {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  width: min(560px, 100%);
  max-height: min(80vh, 720px);
  overflow-y: auto;
  padding: var(--sp-4);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-3);
  color: var(--text-primary);
  font-family: var(--sans);
}
.ic-title { margin: 0; font-family: var(--serif); font-size: var(--fs-lg); color: var(--text-strong); }
.ic-lead, .ic-note, .ic-task, .ic-materials, .ic-consequence { margin: 0; font-size: var(--fs-sm); line-height: 1.65; overflow-wrap: anywhere; }
.ic-lead { color: var(--text-primary); }
.ic-note { color: var(--warning); font-size: var(--fs-xs); }
.ic-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
.ic-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-inset);
}
.ic-task { color: var(--text-strong); }
.ic-materials { color: var(--text-secondary); font-family: var(--mono); font-size: var(--fs-xs); }
.ic-consequence { color: var(--text-secondary); font-size: var(--fs-xs); }
.ic-error { margin: 0; color: var(--danger); font-size: var(--fs-xs); overflow-wrap: anywhere; }
.ic-actions { display: flex; flex-wrap: wrap; gap: var(--sp-2); justify-content: flex-end; }
.ic-btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  padding: 4px var(--sp-3);
  cursor: pointer;
}
.ic-btn.primary { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }
.ic-btn:disabled { opacity: 0.6; cursor: default; }
.ic-btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
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
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-2);
  color: var(--text-primary);
  font-family: var(--sans);
}
.ic-outcome-title { margin: 0; font-family: var(--serif); font-size: var(--fs-sm); color: var(--text-strong); }
.ic-outcome-text, .ic-paused p { margin: 0; font-size: var(--fs-xs); line-height: 1.65; color: var(--text-secondary); overflow-wrap: anywhere; }
.ic-paused ul { margin: 0; padding-left: 1.2em; font-size: var(--fs-xs); color: var(--text-secondary); }
</style>
