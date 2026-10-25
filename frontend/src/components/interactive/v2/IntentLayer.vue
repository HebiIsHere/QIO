<!--
  虚拟意图板面层（Phase 3-A，契约 §6.1 / §6.2）。

  它画的是**确认前**的意图：虚线、轻量、明确写「虚拟 / 确认前不执行真实板面改动」，
  不是成果卡，也不进材料范围。三件事：
  1. 位置按 intent_id 绑定（view.display，后端 D 并行持久化）；没有就确定性落位，绝不用随机数；
     可选中、可拖动；拖动只改显示位置，松手通过 emit("display-change") 交回持久化接口（Lead 接线）；
  2. 单条 / 同批确认都只用**显式 ids**；needs_update / 未知状态 / 已开始的不冒充可确认；
  3. 确认结果逐项反馈（部分失败分开列），只显示服务端真实 turn_id 与 status；「已确认」不写成「已完成」。
     旧工作台 / BatchTray / StatusPopover 的有效能力（依赖、冲突、账本、恢复预览）在详情里迁移保留。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import {
  batchConfirmIntents,
  confirmSingleIntent,
  fetchRecoveryPreview,
  recoverIntent,
  rejectIntent,
} from "../../../services/interactive";
import {
  capabilityRows,
  batchConfirmState,
  clampIntentDisplay,
  confirmBlockReason,
  confirmBlockText,
  displayEquals,
  dragIntentDisplay,
  isNeedsUpdate,
  intentDisplayMap,
  recoveryFacts,
  recoveryResultText,
  summarizeConfirm,
  type ConfirmSummary,
  type IntentDisplayPoint,
  type IntentViewWithDisplay,
  type RecoveryFacts,
  DEFAULT_INTENT_CARD_SIZE,
} from "./intentDisplay";
import { canRecover, isConfirmable, statusLabel, statusTone, type IntentView } from "../../../interactive/intentWorkbench";

const props = withDefaults(
  defineProps<{
    /** 新链意图视图（真实 IntentView；display / turn_id 是 Phase 3 追加字段） */
    views: IntentViewWithDisplay[];
    /** 板面身份；没有时不发任何确认 / 恢复请求（如实禁用，不伪造结果） */
    boardId?: string;
    /** 板面画布尺寸（拖动钳制范围） */
    width: number;
    height: number;
    /** 画布缩放：屏幕位移 ÷ scale = 板面位移 */
    scale?: number;
    /** 外部选中的意图 id（可选；不传就由本层自己管） */
    selectedId?: string | null;
    /** 外部覆盖的显示位置（例如 Lead 的乐观更新） */
    displayOverrides?: Record<string, IntentDisplayPoint> | null;
    /** 只读（例如提交 / 审批进行中） */
    disabled?: boolean;
    /** F10：每张卡的显示位置保存状态（等待保存 / 保存失败），由页面权威状态传入 */
    displayStates?: Record<string, { phase: string; error?: string | null }> | null;
    /**
     * F06：意图清单读取失败时，依赖最新事实的确认 / 恢复必须先核实。
     * true = 本层展示的是上次读到的事实（可疑），确认与恢复暂停并说明原因。
     */
    factsStale?: boolean;
    factsError?: string | null;
  }>(),
  {
    boardId: "",
    scale: 1,
    selectedId: null,
    displayOverrides: null,
    disabled: false,
    displayStates: null,
    factsStale: false,
    factsError: null,
  },
);

const emit = defineEmits<{
  (e: "select", intentId: string | null): void;
  /** 持久化接口：位置按 intent_id 绑定（Lead 写 store / API；本层只做显示） */
  (e: "display-change", payload: { intentId: string; x: number; y: number }): void;
  /** 确认 / 恢复之后让外层重新读取意图视图（真实状态以服务端为准） */
  (e: "refresh"): void;
  (e: "confirmed", summary: ConfirmSummary): void;
  (e: "open-record", intent: IntentView): void;
}>();

const selected = ref<string | null>(props.selectedId ?? null);
watch(
  () => props.selectedId,
  (value) => {
    selected.value = value ?? null;
  },
);

/**
 * 当前手势的**临时位置**（F10）。
 *
 * 只活在一次拖动里：松手把最终位置交给 display-change（父级权威状态立刻接管），
 * 取消 / 指针取消 / 卸载都直接丢弃 —— 所以既不会把未保存的位置永久压住父级更新，
 * 取消拖动也能回到本次手势开始的位置（而不是更早的默认位置）。
 */
const dragPoint = ref<Record<string, IntentDisplayPoint>>({});
const checkedIds = ref<string[]>([]);
const busy = ref("");
const notice = ref("");
const error = ref("");
const summary = ref<ConfirmSummary | null>(null);

const recoveryTargetId = ref("");
const recovery = ref<RecoveryFacts | null>(null);

const displays = computed<Record<string, IntentDisplayPoint>>(() =>
  intentDisplayMap(props.views, { ...(props.displayOverrides ?? {}), ...dragPoint.value }),
);

/** 这张卡的位置保存状态（没有 = 已确认；pending / failed 由页面权威状态给出）。 */
function displayStateOf(intentId: string): { phase: string; error?: string | null } | null {
  return props.displayStates?.[intentId] ?? null;
}

interface RenderedIntent {
  view: IntentViewWithDisplay;
  display: IntentDisplayPoint;
  persisted: boolean;
  tone: string;
  blockReason: ReturnType<typeof confirmBlockReason>;
  blockText: string;
  selectable: boolean;
  recoverable: boolean;
}

const rendered = computed<RenderedIntent[]>(() =>
  (props.views ?? [])
    .filter((view) => view && typeof view.intent_id === "string" && view.intent_id)
    .map((view) => {
      const display = displays.value[view.intent_id] ?? { x: 0, y: 0 };
      const stored = view.display;
      const persisted = Boolean(
        (stored && typeof stored === "object" && Number.isFinite(stored.x as number) && Number.isFinite(stored.y as number)) ||
          (props.displayOverrides && props.displayOverrides[view.intent_id]) ||
          dragPoint.value[view.intent_id],
      );
      const blockReason = confirmBlockReason(view);
      return {
        view,
        display,
        persisted,
        tone: statusTone(view.status),
        blockReason,
        blockText: confirmBlockText(view),
        selectable: blockReason === null,
        recoverable: canRecover(view),
      };
    }),
);

/** F06：事实可疑时给用户看的一致说明（确认 / 恢复 / 拒绝都先停一下）。 */
const staleBanner = computed(
  () =>
    (props.factsError
      ? "意图清单更新失败（" + props.factsError + "）。"
      : "意图清单更新失败。") +
    "界面上是上次读到的事实：确认 / 恢复 / 拒绝会先暂停，请先重试读取最新事实。",
);

function staleText(): string {
  return staleBanner.value + "（已暂停这次动作，不做猜测，也不会自动重试成确认。）";
}

/** 拒绝能力保留：只对旧链存储里的意图可用（raw_legacy_status 存在即证明该 id 属于旧链）。 */
function canReject(view: IntentViewWithDisplay | null | undefined): boolean {
  if (!view) return false;
  return Boolean(view.raw_legacy_status) || isNeedsUpdate(view);
}

const batch = computed(() => batchConfirmState(props.views, checkedIds.value));
const selectedView = computed(() => rendered.value.find((item) => item.view.intent_id === selected.value) ?? null);
const selectedRows = computed(() => (selectedView.value ? capabilityRows(selectedView.value.view) : []));
const canAct = computed(() => Boolean(props.boardId) && !props.disabled);

function errorMessage(cause: unknown): string {
  return (cause as Error)?.message ?? String(cause) ?? "原因未知";
}

function isChecked(intentId: string): boolean {
  return checkedIds.value.includes(intentId);
}

function toggleChecked(intentId: string): void {
  if (!rendered.value.find((item) => item.view.intent_id === intentId)?.selectable) return;
  checkedIds.value = isChecked(intentId)
    ? checkedIds.value.filter((item) => item !== intentId)
    : [...checkedIds.value, intentId];
}

function select(intentId: string | null): void {
  selected.value = intentId;
  recoveryTargetId.value = "";
  recovery.value = null;
  emit("select", intentId);
}

/* ---- 拖动：只改显示位置（位置按 intent_id 绑定，松手交回持久化接口） ---- */
interface DragState {
  intentId: string;
  startClient: { x: number; y: number };
  startDisplay: IntentDisplayPoint;
  moved: boolean;
}
const drag = ref<DragState | null>(null);

function onCardPointerDown(view: IntentViewWithDisplay, event: PointerEvent): void {
  if (props.disabled) return;
  if (event.button !== undefined && event.button !== 0) return;
  const start = displays.value[view.intent_id] ?? { x: 0, y: 0 };
  drag.value = {
    intentId: view.intent_id,
    startClient: { x: event.clientX, y: event.clientY },
    startDisplay: { x: start.x, y: start.y },
    moved: false,
  };
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp);
  window.addEventListener("pointercancel", onPointerCancel);
}

function onPointerMove(event: PointerEvent): void {
  const state = drag.value;
  if (!state) return;
  const dx = event.clientX - state.startClient.x;
  const dy = event.clientY - state.startClient.y;
  if (!state.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
  state.moved = true;
  dragPoint.value = {
    ...dragPoint.value,
    [state.intentId]: dragIntentDisplay(state.startDisplay, { dx, dy }, {
      bounds: { w: props.width, h: props.height },
      scale: props.scale,
      cardSize: DEFAULT_INTENT_CARD_SIZE,
    }),
  };
}

/** 结束本次手势的临时位置：之后由父级权威状态（overrides）决定画在哪。 */
function clearDragPoint(intentId: string): void {
  if (!Object.prototype.hasOwnProperty.call(dragPoint.value, intentId)) return;
  const next = { ...dragPoint.value };
  delete next[intentId];
  dragPoint.value = next;
}

function onPointerUp(): void {
  const state = drag.value;
  detachDrag();
  if (!state) return;
  if (!state.moved) {
    clearDragPoint(state.intentId);
    select(state.intentId);
    return;
  }
  const point = dragPoint.value[state.intentId];
  clearDragPoint(state.intentId);
  if (!point) return;
  emit("display-change", { intentId: state.intentId, x: point.x, y: point.y });
}

function onPointerCancel(): void {
  const state = drag.value;
  detachDrag();
  // 取消拖动 = 回到本次手势开始的位置（父级 overrides 仍是手势开始那一刻的值）
  if (state) clearDragPoint(state.intentId);
}

function detachDrag(): void {
  drag.value = null;
  window.removeEventListener("pointermove", onPointerMove);
  window.removeEventListener("pointerup", onPointerUp);
  window.removeEventListener("pointercancel", onPointerCancel);
}

onBeforeUnmount(detachDrag);

/* ---- 确认（显式 ids；部分失败逐项反馈；只认真实 turn_id / status） ---- */
async function confirmOne(view: IntentViewWithDisplay): Promise<void> {
  error.value = "";
  notice.value = "";
  if (props.factsStale) {
    error.value = staleText();
    return;
  }
  const block = confirmBlockReason(view);
  if (block) {
    error.value = confirmBlockText(view);
    return;
  }
  if (!canAct.value) {
    error.value = "没有板面身份或正在处理：不能确认（不会伪造结果）。";
    return;
  }
  if (busy.value) return;
  busy.value = view.intent_id;
  try {
    const response = await confirmSingleIntent(view.intent_id, props.boardId);
    const result = summarizeConfirm(response, [view.intent_id], { batch: false });
    summary.value = result;
    notice.value = result.notice;
    emit("confirmed", result);
    emit("refresh");
  } catch (cause) {
    error.value = "确认没有完成：" + errorMessage(cause);
  } finally {
    busy.value = "";
  }
}

async function confirmBatch(): Promise<void> {
  error.value = "";
  notice.value = "";
  if (props.factsStale) {
    error.value = staleText();
    return;
  }
  const state = batch.value;
  if (!state.enabled) {
    error.value = state.hint;
    return;
  }
  if (!canAct.value) {
    error.value = "没有板面身份或正在处理：不能确认（不会伪造结果）。";
    return;
  }
  if (busy.value) return;
  busy.value = "batch";
  try {
    // 显式 ids：只提交可确认的已选项，顺序稳定；不足两条不会到这里
    const response = await batchConfirmIntents(props.boardId, state.ids);
    const result = summarizeConfirm(response, state.ids, { batch: true });
    summary.value = result;
    notice.value = result.notice;
    checkedIds.value = [];
    emit("confirmed", result);
    emit("refresh");
  } catch (cause) {
    error.value = "同批确认没有完成：" + errorMessage(cause);
  } finally {
    busy.value = "";
  }
}

/* ---- 恢复入口（旧工作台能力迁移；只回到可继续状态，不自动执行） ---- */
async function openRecovery(view: IntentViewWithDisplay): Promise<void> {
  if (!canRecover(view)) return;
  if (props.factsStale) {
    error.value = staleText();
    return;
  }
  recoveryTargetId.value = view.intent_id;
  recovery.value = null;
  try {
    const response = await fetchRecoveryPreview(view.intent_id);
    recovery.value = recoveryFacts(response?.preview, response?.available !== false);
  } catch (cause) {
    recovery.value = {
      available: false,
      canResume: false,
      reasons: [],
      operations: [],
      dependencies: [],
      text: "读取恢复预览失败：" + errorMessage(cause),
    };
  }
}

async function confirmRecovery(view: IntentViewWithDisplay): Promise<void> {
  const facts = recovery.value;
  if (!facts || !facts.canResume || busy.value) return;
  if (props.factsStale) {
    error.value = staleText();
    return;
  }
  if (!props.boardId) {
    error.value = "没有板面身份：不能恢复（不会伪造结果）。";
    return;
  }
  busy.value = "recover:" + view.intent_id;
  error.value = "";
  try {
    const result = await recoverIntent(view.intent_id, props.boardId, view.version);
    notice.value = recoveryResultText(result);
    recoveryTargetId.value = "";
    recovery.value = null;
    emit("refresh");
  } catch (cause) {
    error.value = "恢复没有完成：" + errorMessage(cause);
  } finally {
    busy.value = "";
  }
}

/**
 * 拒绝（能力保留，不在迁移中丢失）：调用既有 /intents/{id}/reject。
 *
 * 只对旧链存储里的待处理意图出现；拒绝后板面原内容保留、不会执行，
 * 结果以服务端返回的真实状态为准，然后交回外层刷新意图事实。
 */
async function rejectOne(view: IntentViewWithDisplay): Promise<void> {
  error.value = "";
  notice.value = "";
  if (props.factsStale) {
    error.value = staleText();
    return;
  }
  if (!canReject(view)) {
    error.value = "这条意图不在旧链存储里：走对应的确认 / 取消流程，不用拒绝接口。";
    return;
  }
  if (!canAct.value) {
    error.value = "没有板面身份或正在处理：不能拒绝（不会伪造结果）。";
    return;
  }
  if (busy.value) return;
  busy.value = "reject:" + view.intent_id;
  try {
    await rejectIntent(view.intent_id);
    notice.value = "已拒绝：" + (view.title || view.intent_id) + "（板面原内容保留，不会执行）。";
    emit("refresh");
  } catch (cause) {
    error.value = "拒绝没有完成：" + errorMessage(cause);
  } finally {
    busy.value = "";
  }
}

/** 供测试 / 外层读当前显示位置（不改任何状态） */
function displayOf(intentId: string): IntentDisplayPoint | null {
  return displays.value[intentId] ?? null;
}

defineExpose({ displayOf, select, confirmOne, confirmBatch });
</script>

<template>
  <section class="intent-layer" data-im="intent-layer" aria-label="虚拟意图（确认前不执行）">
    <p class="layer-hint" data-im="intent-layer-hint">
      虚拟意图（虚线）：确认前不执行真实板面改动；拖动只改预览位置，成果落点以服务端为准。
    </p>

    <p v-if="props.factsStale" class="stale" role="alert" data-im="intent-stale">
      {{ staleBanner }}
      <button class="btn" type="button" data-im="intent-stale-retry" @click="emit('refresh')">重试读取意图清单</button>
    </p>

    <div class="batch-bar" data-im="intent-batch-bar">
      <span class="batch-hint" data-im="intent-batch-hint">{{ batch.hint }}</span>
      <button
        class="btn primary"
        type="button"
        data-im="intent-batch-confirm"
        :disabled="!batch.enabled || !canAct || Boolean(busy) || props.factsStale"
        :title="batch.hint"
        @click="confirmBatch"
      >{{ busy === "batch" ? "提交中…" : "同批确认 " + batch.count + " 条" }}</button>
    </div>

    <p v-if="notice" class="notice" role="status" data-im="intent-layer-notice">{{ notice }}</p>
    <p v-if="error" class="error" role="alert" data-im="intent-layer-error">{{ error }}</p>

    <div class="objects">
      <article
        v-for="item in rendered"
        :key="item.view.intent_id"
        class="virtual-intent"
        :class="[item.tone, { selected: selected === item.view.intent_id }]"
        :style="{ left: item.display.x + 'px', top: item.display.y + 'px', width: DEFAULT_INTENT_CARD_SIZE.w + 'px', minHeight: DEFAULT_INTENT_CARD_SIZE.h + 'px' }"
        data-im="virtual-intent"
        data-virtual="1"
        :data-intent-id="item.view.intent_id"
        :data-status="item.view.status"
        :data-selected="selected === item.view.intent_id ? 'true' : 'false'"
        :data-display-persisted="item.persisted ? 'true' : 'false'"
        :data-display-state="displayStateOf(item.view.intent_id)?.phase ?? 'confirmed'"
        @pointerdown="onCardPointerDown(item.view, $event)"
      >
        <header class="vi-head">
          <span class="vi-badge">虚拟</span>
          <span class="vi-title">{{ item.view.title || "待确认意图" }}</span>
          <span class="vi-status" :class="item.tone" data-im="virtual-intent-status">{{ statusLabel(item.view.status) }}</span>
          <label v-if="item.selectable" class="vi-check" @pointerdown.stop>
            <input
              type="checkbox"
              data-im="intent-check"
              :checked="isChecked(item.view.intent_id)"
              @change="toggleChecked(item.view.intent_id)"
            />
            选择
          </label>
        </header>
        <p v-if="item.blockReason" class="vi-block" data-im="intent-block">{{ item.blockText }}</p>
        <p v-else class="vi-note" data-im="intent-virtual-note">确认前不执行；确认后由服务端续作（本轮不代表已完成）。</p>
        <p
          v-if="displayStateOf(item.view.intent_id)?.phase === 'pending'"
          class="vi-meta mono"
          data-im="intent-display-pending"
        >位置等待保存…</p>
        <p
          v-else-if="displayStateOf(item.view.intent_id)?.phase === 'failed'"
          class="vi-meta warn"
          data-im="intent-display-failed"
        >位置保存失败：{{ displayStateOf(item.view.intent_id)?.error }}</p>
        <p v-if="item.view.operations_summary" class="vi-meta mono" data-im="intent-operations-summary">
          操作：已完成 {{ item.view.operations_summary.applied ?? 0 }} · 执行中 {{ item.view.operations_summary.running ?? 0 }} ·
          未开始 {{ item.view.operations_summary.pending ?? 0 }} · 失败 {{ item.view.operations_summary.failed ?? 0 }}
        </p>
        <p v-if="item.view.raw_legacy_status" class="vi-meta mono" data-im="intent-legacy-status">
          旧状态 {{ item.view.raw_legacy_status }}（只读，不冒充新状态）
        </p>
      </article>
    </div>

    <aside v-if="selectedView" class="detail" data-im="intent-detail" :data-intent-id="selectedView.view.intent_id">
      <header class="detail-head">
        <span class="detail-title">{{ selectedView.view.title || "待确认意图" }}</span>
        <span class="vi-status" :class="selectedView.tone" data-im="intent-detail-status">{{ statusLabel(selectedView.view.status) }}</span>
        <span class="detail-version mono" data-im="intent-detail-version">v{{ selectedView.view.version }}</span>
        <button class="btn quiet" type="button" data-im="intent-detail-close" @click="select(null)">关闭</button>
      </header>

      <p v-if="selectedView.view.goal" class="row" data-im="intent-goal"><span class="label">目标</span>{{ selectedView.view.goal }}</p>
      <p v-if="selectedView.view.expected" class="row" data-im="intent-expected"><span class="label">预期</span>{{ selectedView.view.expected }}</p>
      <p
        v-for="row in selectedRows"
        :key="row.key"
        class="row"
        :class="'tone-' + row.tone"
        :data-im="'intent-row:' + row.key"
      ><span class="label">{{ row.label }}</span>{{ row.text }}</p>

      <p v-if="selectedView.blockReason" class="warn" data-im="intent-needs-update">{{ selectedView.blockText }}</p>
      <p v-else-if="isConfirmable(selectedView.view)" class="hint" data-im="intent-confirm-hint">
        确认只提交这一条（显式 id），确认后以服务端返回的执行轮次与状态为准；已确认不等于已完成。
      </p>

      <div class="detail-actions">
        <button
          class="btn primary"
          type="button"
          data-im="intent-confirm"
          :disabled="!selectedView.selectable || !canAct || Boolean(busy) || props.factsStale"
          @click="confirmOne(selectedView.view)"
        >{{ busy === selectedView.view.intent_id ? "提交中…" : "确认这一条" }}</button>
        <button
          v-if="canReject(selectedView.view)"
          class="btn"
          type="button"
          data-im="intent-reject"
          :disabled="!canAct || Boolean(busy) || props.factsStale"
          title="拒绝这条待处理意图（板面原内容保留，不会执行）"
          @click="rejectOne(selectedView.view)"
        >{{ busy === "reject:" + selectedView.view.intent_id ? "拒绝中…" : "拒绝" }}</button>
        <button
          v-if="selectedView.recoverable"
          class="btn"
          type="button"
          data-im="intent-recover-preview"
          :disabled="Boolean(busy)"
          @click="openRecovery(selectedView.view)"
        >恢复预览</button>
        <button
          class="btn quiet"
          type="button"
          data-im="intent-execution-record"
          @click="emit('open-record', selectedView.view)"
        >执行记录</button>
      </div>

      <div v-if="recoveryTargetId === selectedView.view.intent_id && recovery" class="recovery" data-im="intent-recovery">
        <p class="row" data-im="intent-recovery-text">{{ recovery.text }}</p>
        <div class="detail-actions">
          <button
            class="btn primary"
            type="button"
            data-im="intent-recover-confirm"
            :disabled="!recovery.canResume || !canAct || Boolean(busy)"
            @click="confirmRecovery(selectedView.view)"
          >确认恢复</button>
          <button class="btn quiet" type="button" data-im="intent-recover-cancel" @click="recoveryTargetId = ''">取消</button>
        </div>
      </div>
    </aside>

    <div v-if="summary" class="summary" data-im="intent-confirm-summary">
      <p class="row" data-im="intent-confirm-notice">{{ summary.notice }}</p>
      <ul class="rows" data-im="intent-confirm-rows">
        <li
          v-for="row in summary.rows"
          :key="row.intentId"
          data-im="intent-confirm-row"
          :data-intent-id="row.intentId"
          :data-ok="row.ok ? 'true' : 'false'"
          :data-turn-id="row.turnId ?? ''"
          :data-completed="row.completed ? 'true' : 'false'"
        >{{ row.intentId }} · {{ row.statusText }}<span v-if="row.turnId"> · 执行轮次 {{ row.turnId }}</span><span v-if="row.reason"> · {{ row.reason }}</span></li>
      </ul>
      <p v-if="summary.failedIds.length" class="warn" data-im="intent-confirm-partial">
        部分失败：{{ summary.failedIds.join("、") }}
      </p>
      <p v-if="summary.startedUnknownIds.length" class="warn" data-im="intent-confirm-started-unknown">
        没有返回执行轮次：{{ summary.startedUnknownIds.join("、") }}（不能认为已经开始执行）
      </p>
    </div>
  </section>
</template>

<style scoped>
.intent-layer {
  position: absolute;
  inset: 0;
  pointer-events: none; /* 整层不挡正式卡片；只有虚拟卡与详情可交互 */
  z-index: 26;
  font-family: var(--sans);
}
.layer-hint,
.batch-bar,
.notice,
.error,
.summary {
  pointer-events: auto;
}
.layer-hint {
  position: absolute;
  left: var(--sp-2);
  top: var(--sp-2);
  margin: 0;
  padding: 2px var(--sp-2);
  border: 1px dashed var(--link);
  border-radius: var(--r-pill);
  background: var(--bg-surface);
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.batch-bar {
  position: absolute;
  left: var(--sp-2);
  top: calc(var(--sp-2) + 24px);
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  max-width: min(560px, 90%);
  padding: var(--sp-1) var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  background: var(--bg-elevated);
}
.batch-hint { font-size: var(--fs-xs); color: var(--text-secondary); }
.stale {
  position: absolute;
  left: var(--sp-2);
  top: calc(var(--sp-2) + 66px);
  max-width: min(620px, 90%);
  margin: 0;
  padding: var(--sp-1) var(--sp-2);
  border: 1px solid var(--warning);
  border-radius: var(--r-lg);
  background: var(--bg-elevated);
  color: var(--warning);
  font-size: var(--fs-xs);
  line-height: 1.6;
  pointer-events: auto;
}
.stale .btn { margin-left: var(--sp-2); }
.objects { position: absolute; inset: 0; pointer-events: none; }
.virtual-intent {
  position: absolute;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-2);
  border: 1px dashed var(--link);
  border-radius: var(--r-lg);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: var(--fs-sm);
  opacity: 0.94;
  cursor: grab;
  pointer-events: auto;
  user-select: none;
}
.virtual-intent.selected { border-style: solid; box-shadow: var(--shadow-1); }
.virtual-intent.settled { border-color: var(--success); }
.virtual-intent.problem { border-color: var(--danger); }
.virtual-intent.waiting { border-color: var(--warning); }
.vi-head { display: flex; align-items: center; gap: var(--sp-1); flex-wrap: wrap; }
.vi-badge {
  font-size: var(--fs-xs);
  color: var(--on-accent);
  background: var(--link);
  border-radius: var(--r-pill);
  padding: 0 var(--sp-1);
}
.vi-title { font-size: var(--fs-sm); color: var(--text-strong); }
.vi-status {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  padding: 0 var(--sp-1);
}
.vi-status.open { color: var(--link); }
.vi-status.waiting { color: var(--warning); }
.vi-status.active { color: var(--accent); }
.vi-status.settled { color: var(--success); }
.vi-status.problem { color: var(--danger); }
.vi-check { margin-left: auto; display: inline-flex; align-items: center; gap: 2px; font-size: var(--fs-xs); color: var(--text-secondary); }
.vi-block, .warn { margin: 0; color: var(--warning); font-size: var(--fs-xs); line-height: 1.6; }
.vi-note, .hint { margin: 0; color: var(--text-muted); font-size: var(--fs-xs); line-height: 1.6; }
.vi-meta { margin: 0; color: var(--text-muted); font-size: var(--fs-xs); }
.detail {
  position: absolute;
  right: var(--sp-2);
  top: var(--sp-2);
  width: min(360px, 92%);
  max-height: 70%;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  background: var(--bg-panel);
  box-shadow: var(--shadow-2);
  pointer-events: auto;
}
.detail-head { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.detail-title { font-family: var(--serif); font-size: var(--fs-sm); color: var(--text-strong); }
.detail-version { font-size: var(--fs-xs); color: var(--text-muted); }
.row { margin: 0; font-size: var(--fs-xs); line-height: 1.65; overflow-wrap: anywhere; color: var(--text-primary); }
.label { display: inline-block; margin-right: var(--sp-1); color: var(--text-muted); }
.tone-problem { color: var(--danger); }
.tone-waiting { color: var(--warning); }
.detail-actions { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.recovery { border: 1px dashed var(--border-strong); border-radius: var(--r-lg); padding: var(--sp-1); display: flex; flex-direction: column; gap: var(--sp-1); }
.notice, .error {
  position: absolute;
  left: var(--sp-2);
  bottom: var(--sp-2);
  max-width: min(620px, 92%);
  margin: 0;
  padding: var(--sp-1) var(--sp-2);
  border-radius: var(--r-lg);
  font-size: var(--fs-xs);
  z-index: 2;
}
.notice { color: var(--success); background: var(--success-soft); border: 1px solid var(--success-soft); }
.error { color: var(--danger); background: var(--danger-soft); border: 1px solid var(--border-danger); }
.summary {
  position: absolute;
  right: var(--sp-2);
  bottom: var(--sp-2);
  width: min(420px, 92%);
  max-height: 40%;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  background: var(--bg-elevated);
}
.rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.rows li { font-size: var(--fs-xs); color: var(--text-secondary); }
.btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: none;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  padding: 2px var(--sp-2);
  cursor: pointer;
  pointer-events: auto;
}
.btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
.btn:disabled { opacity: 0.5; cursor: default; }
.btn.primary { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }
.btn.primary:hover { background: var(--accent-hover); color: var(--on-accent); }
.btn.quiet { border-color: transparent; }
.mono { font-family: var(--mono); }
</style>
