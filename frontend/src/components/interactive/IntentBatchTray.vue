<!--
  右上角批量审批入口 + 跨浮层几何控制器（子智能体 D）。

  契约 §9.3 / §9.6：
  - 资格由 approval.batchesWithList 判定（该批总量 ≥4 且还有未处理项）；这里只显示结果，
    不自己重新定义「几项才算一批」；
  - 列表**显示剩余待处理数量**；已经处理过的项灰掉、不能再被勾选或提交；
    全部处理完后由 batchesWithList 不再返回该批，入口随之消失；
  - 开合完全由用户决定：本组件不写任何「数量变化就展开 / 收起」的逻辑；
  - 内容更新（处理掉几项、又来一批）时保留阅读位置，不跳回顶部；
  - 不同批次的未处理意图不累加：判定在 interactive/approval.ts；
  - 点击条目定位到板面上的虚线预览（window 事件 qio:interactive:locate-preview）。

  本组件同时是**跨浮层几何的运行时施加者**（几何计划见 interactive/overlayLayout.ts）：
  - 契约 §11.7：几何一变（首次挂载 / 恢复开合 / 窗口缩放 / 工具栏变高）就按**最新几何**落实
    「空间不足只展开一个面板」，并给出明确的切换入口；从小窗口恢复大窗口**不会自动弹出**面板；
    需要切换显示时**始终把同一个状态**（cramped）传给几何层，所以关掉一个面板后切换条那一行
    仍然被真实预留；切换条的位置与高度直接取几何计划给的矩形，不在组件里另算一套；
  它挂在板面舞台上、又是聊天与批量面板共同祖先的直接子节点，所以把量到的数字算成
  --im-geo-chat-* / --im-geo-batch-* 写在舞台上，兄弟组件（ChatDock 的面板）通过
  styles/interactive-shell.css 读取这些变量，不需要改它的源码。
  为什么不会来回抖动：只观察**舞台**与**底部工具栏**（两者的尺寸都不受浮层影响），
  写入前先比较字符串；面板自己的尺寸永远是被写的一方，不进观察链路。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import {
  BATCH_LIST_MIN,
  batchEntryText,
  groupIntentsByBatch,
  batchSummaryIn,
  clearBatchSelectionIn,
  locatePreview,
  previewBounds,
  pruneBatchSelection,
  selectAllInBatch,
  splitBatchIn,
  statusText,
  toggleBatchSelectionIn,
  type IntentBatch,
} from "../../interactive/approval";
import {
  OVERLAY_EDGE,
  OVERLAY_GAP,
  chatHeightRelaxation,
  chatSwitchLift,
  overlaysAreCramped,
  planOverlayGeometry,
  resolveOverlayPanes,
  type OverlayGeometry,
  type OverlayInput,
  type OverlayPane,
} from "../../interactive/overlayLayout";
import type { Intent } from "../../interactive/types";

const store = useInteractiveStore();

/**
 * 需要提供批量列表的批次（契约 §9.3）：
 * - 资格看**该批产生的意图总量** ≥ BATCH_LIST_MIN，不是「每次剩余的待审批数」——
 *   处理掉其中几项之后入口与列表继续保留，直到没有未处理项；
 * - 全部处理完（pendingIds 为空）时这里不再返回该批，入口随之消失；
 * - 不同批次不合并：分组仍然只用 approval.groupIntentsByBatch。
 *
 * 阈值的常量、批次分组都取自 approval.ts（B 维护），这里只组合一次方向由 §9.3 规定的资格条件。
 * 集成后如果 batchesWithList 已经落地同一条规则，两者的结果完全相同（用例里两条路径都断言过）。
 */
const batches = computed(() =>
  groupIntentsByBatch(store.intents).filter(
    (batch) => batch.intentIds.length >= BATCH_LIST_MIN && batch.pendingIds.length > 0,
  ),
);
/** 每一批的选中项各自独立（键 = 批次 key） */
const selectedByBatch = ref<Record<string, string[]>>({});
const busy = ref(false);
const notice = ref<string | null>(null);
const error = ref<string | null>(null);

/**
 * 设计下限：低于这个尺寸就读不成（聊天：一条消息 + 输入区；批量：说明 + 两行条目 + 按钮）。
 * 几何层保证「放得下就不小于它」，放不下就明确切换，而不是把面板压到看不见。
 */
const CHAT_MIN = { width: 320, height: 240 };
const BATCH_MIN = { width: 300, height: 220 };

const trayEl = ref<HTMLElement | null>(null);
const panelEl = ref<HTMLElement | null>(null);

/** 入口按钮是 v-for 出来的（可能有多批），用查询而不是模板 ref：模板 ref 在 v-for 里会变成数组 */
function entryElement(): HTMLElement | null {
  return trayEl.value?.querySelector<HTMLElement>('[data-im="batch-entry"]') ?? null;
}

/** 当前几何（验收脚本可读；同时也决定了面板的尺寸上限与位置） */
const geometry = ref<OverlayGeometry | null>(null);
/**
 * 空间不够同时放两个面板（几何返回 switched）。注意：只有一侧打开时也返回 side-by-side，
 * 所以判据是「两个都当作打开」再算一次（overlaysAreCramped），与当前开合无关。
 *
 * 这个值**每次测量都重算**；落实面板切换也只用这一轮的新值，不用上一轮留下的结果。
 */
const cramped = ref(false);
/** 空间不够时保留哪一个面板展开：按用户最近打开的那个算（见下面的 watch） */
const activePane = ref<OverlayPane>("chat");
/**
 * 切换条的视口坐标（position: fixed；高度是常量，避免观察自己造成重排）。
 *
 * 数字全部来自几何计划给出的 {@link OverlayGeometry.switchBar} 矩形：
 * 组件不自己推「切换条该在哪」，否则面板让出的位置与切换条实际画的位置会漂移。
 */
const switchBarStyle = ref<Record<string, string>>({});
/**
 * 切换条是否可见：几何说需要切换显示，而且确实还有一个面板开着（没有面板就没有可切换的对象）。
 * 判据与预留用的是**同一个** geometry.mode，所以不会出现「留了位置却没有条」或反过来的情况。
 */
const switchBarVisible = computed(
  () => geometry.value?.mode === "switched" && (store.chatOpen || store.batchOpen),
);

const trayStyle = ref<Record<string, string>>({});
const panelStyle = ref<Record<string, string>>({});
/** 上一次真正写进 DOM 的几何串：相同就不写，避免观察自身尺寸导致的持续重排 */
let appliedKey = "";

function measureToolbarTop(stageBottom: number): number {
  if (typeof document === "undefined") return stageBottom;
  const bar = document.querySelector('[data-im="board-toolbar"]');
  if (!bar) return stageBottom;
  const rect = bar.getBoundingClientRect();
  if (rect.height <= 0) return stageBottom;
  return rect.top;
}

function measureToggleHeight(): number {
  if (typeof document === "undefined") return 0;
  const toggle = document.querySelector('[data-im="chat-toggle"]');
  if (!toggle) return 0;
  return toggle.getBoundingClientRect().height;
}

function overlayInput(stageRect: DOMRect, toolbarTop: number, chatOpen: boolean, batchOpen: boolean): OverlayInput {
  return {
    viewport: { width: window.innerWidth, height: window.innerHeight },
    stage: { top: stageRect.top, height: stageRect.height },
    toolbarTop,
    chatOpen,
    batchOpen,
    chatMin: CHAT_MIN,
    batchMin: BATCH_MIN,
  };
}

const stageEl = ref<HTMLElement | null>(null);

/**
 * 落地「空间不足只保留一个面板」（契约 §10.6），并把结果写回 store。
 *
 * 幂等：只有在结果与当前开合不同时才写 —— 写完会再触发一次测量，那一轮就没有可改的了，
 * 所以不会出现开合来回反弹。只关闭、不打开：空间变宽时这里什么都不做。
 */
function settlePanes(crampedNow: boolean): ReturnType<typeof resolveOverlayPanes> {
  const resolved = resolveOverlayPanes({
    cramped: crampedNow,
    chatOpen: store.chatOpen,
    batchOpen: store.batchOpen,
    preferred: activePane.value,
    batchAvailable: batches.value.length > 0,
  });
  if (resolved.chatOpen !== store.chatOpen || resolved.batchOpen !== store.batchOpen) {
    store.chatOpen = resolved.chatOpen;
    store.batchOpen = resolved.batchOpen;
  }
  return resolved;
}

/** 量一次实际可用区域并施加几何。只读舞台 / 工具栏 / 聊天入口按钮，都是不受浮层尺寸影响的东西。 */
function applyGeometry(): void {
  if (typeof window === "undefined" || typeof document === "undefined") return;
  const tray = trayEl.value;
  const stage = tray?.parentElement ?? null;
  if (!tray || !stage) return;
  stageEl.value = stage;
  const stageRect = stage.getBoundingClientRect();
  const toolbarTop = measureToolbarTop(stageRect.bottom);
  const chatInput = overlayInput(stageRect, toolbarTop, store.chatOpen, store.batchOpen);
  // ★ 先按**本次量到的**几何判定空间并落实开合，再算几何变量；
  //   绝不能拿上一轮的 cramped 决定（那正是「480px 下两个面板都还开着」的原因）
  cramped.value = overlaysAreCramped(chatInput);
  const resolved = settlePanes(cramped.value);
  // ★ switched 显式传给几何层：这是**同一个明确布局状态**，与当前开着几个面板无关，
  //   所以用户关掉一个面板之后切换条那一行仍然被真实预留（契约 §11.7）。
  // ★ batchAvailable 与 settlePanes 用**同一个**判据（确实存在一个可展示的批次）：
  //   没有审批列表时就没有「另一个可去的面板」，几何层会退出切换布局 ——
  //   不预留、不出虚假切换提示，聊天自己用满可用高度（契约 §12.6）。
  const batchAvailable = batches.value.length > 0;
  const plan = planOverlayGeometry({
    ...chatInput,
    chatOpen: resolved.chatOpen,
    batchOpen: resolved.batchOpen,
    switched: cramped.value,
    batchAvailable,
  });

  // 切换条的位置与高度直接取几何计划的矩形（视口坐标）：面板底边已在同一次计划里让开它
  const bar = plan.switchBar;
  const nextBarStyle: Record<string, string> =
    bar.height > 0
      ? {
          right: OVERLAY_EDGE + "px",
          bottom: Math.round(window.innerHeight - (bar.y + bar.height)) + "px",
          height: bar.height + "px",
          maxWidth: bar.width + "px",
        }
      : {};
  if (!sameStyle(switchBarStyle.value, nextBarStyle)) switchBarStyle.value = nextBarStyle;

  const toggleHeight = measureToggleHeight();
  const chatMaxHeight = plan.chatMaxHeight + chatHeightRelaxation(toggleHeight);
  // 切换显示时把聊天面板抬到切换条上方（ChatDock 的面板底边由它自己的定位决定，
  // 只压 max-height 会让它从自己的底边向上长、仍然压住切换条 —— 实机实测过相交 11934px²）
  const chatLift = plan.mode === "switched" ? chatSwitchLift(toggleHeight) : 0;
  const entryHeight = entryElement()?.getBoundingClientRect().height ?? 0;
  const panelMaxHeight = Math.max(
    140,
    plan.batchMaxHeight - (entryHeight > 0 ? Math.round(entryHeight) + OVERLAY_GAP : 0),
  );
  const key = [
    plan.mode,
    plan.chatMaxWidth,
    chatMaxHeight,
    chatLift,
    plan.batchMaxWidth,
    panelMaxHeight,
    plan.batchRight,
    plan.gap,
    entryHeight > 0 ? 1 : 0,
  ].join("|");
  if (key === appliedKey) return; // 写前比较：同一个几何不重复写 DOM
  appliedKey = key;

  const vars: Record<string, string> = {
    "im-geo-mode": plan.mode,
    "--im-geo-chat-max-w": plan.chatMaxWidth + "px",
    "--im-geo-chat-max-h": chatMaxHeight + "px",
    "--im-geo-batch-max-w": plan.batchMaxWidth + "px",
    "--im-geo-batch-max-h": panelMaxHeight + "px",
    "--im-geo-batch-right": plan.batchRight + "px",
    "--im-geo-gap": plan.gap + "px",
    "--im-geo-chat-lift": chatLift + "px",
  };
  for (const [name, value] of Object.entries(vars)) {
    if (stage.style.getPropertyValue(name) === value) continue;
    if (name.startsWith("--")) stage.style.setProperty(name, value);
    else stage.setAttribute(name, value);
  }

  geometry.value = plan;
  trayStyle.value = { right: plan.batchRight + "px", maxWidth: plan.batchMaxWidth + "px" };
  panelStyle.value = { maxWidth: plan.batchMaxWidth + "px", maxHeight: panelMaxHeight + "px" };

  // 入口按钮可能在写入新宽度后换行：下一帧再量一次（第二次相同就不会再写）
  scheduleMeasure();
}

/** 写回前先比字符串：同一个几何不重复写，避免无意义的渲染（契约 §11.7 不许持续抖动） */
function sameStyle(current: Record<string, string>, next: Record<string, string>): boolean {
  const keys = Object.keys(current);
  if (keys.length !== Object.keys(next).length) return false;
  return keys.every((key) => current[key] === next[key]);
}

let measureTimer: number | null = null;
function scheduleMeasure(): void {
  if (typeof window === "undefined") return;
  if (measureTimer !== null) return;
  measureTimer = window.setTimeout(() => {
    measureTimer = null;
    applyGeometry();
  }, 0);
}

/** 用户明确点「看对话 / 看审批列表」：切换过去，另一个收起（不产生第二套状态） */
function showPane(pane: OverlayPane): void {
  activePane.value = pane;
  store.chatOpen = pane === "chat";
  store.batchOpen = pane === "batch";
  scheduleMeasure();
}

// --- 开合只由用户决定；这里只记录「最近打开的是哪一个」，供空间不足时决定保留谁 -----
// 用一次 watch 同时看两个：无论用户打开哪一个，都按「最近打开」更新，并在**当次**就落实切换。
watch(
  () => [store.chatOpen, store.batchOpen] as const,
  ([chatOpen, batchOpen], [wasChat, wasBatch]) => {
    if (chatOpen && !wasChat) activePane.value = "chat";
    if (batchOpen && !wasBatch) activePane.value = "batch";
    // 空间判定用最近一次量到的几何（几何本身没变，不必等下一次测量）；
    // 真正的「窗口缩放 / 工具栏变高」走 applyGeometry，那里一定用最新量到的数字。
    settlePanes(cramped.value);
    scheduleMeasure();
  },
);
watch(batches, () => {
  scheduleMeasure();
});

// --- 阅读位置：内容更新时不跳回顶部 -----------------------------------------
const readTop = ref(0);
function onPanelScroll(): void {
  readTop.value = panelEl.value?.scrollTop ?? 0;
}

/** 面板开合时把焦点交给面板本身（打开）或入口按钮（收起），键盘用户不会丢位置 */
const panelIsOpen = computed(() => store.batchOpen && batches.value.length > 0);

watch(panelIsOpen, async (open, was) => {
  await nextTick();
  if (open && !was) panelEl.value?.focus?.({ preventScroll: true });
  else if (!open && was) entryElement()?.focus?.({ preventScroll: true });
});

watch(panelIsOpen, async () => {
  await nextTick();
  const el = panelEl.value;
  if (!el) return;
  const want = readTop.value;
  if (want <= 0) return;
  const max = Math.max(0, el.scrollHeight - el.clientHeight);
  const next = Math.min(want, max);
  if (Math.abs(el.scrollTop - next) > 0.5) el.scrollTop = next;
});

/** 键盘：浮层内不触发板面手势；Escape 先关浮层（不外泄给画布） */
function onKeydown(event: KeyboardEvent): void {
  if (event.key !== "Escape") return;
  if (store.batchOpen) {
    store.batchOpen = false;
    event.preventDefault();
  }
}

let resizeObserver: ResizeObserver | null = null;
let toolbarObserver: ResizeObserver | null = null;
let toolbarEl: Element | null = null;

const onWindowResize = () => scheduleMeasure();

onMounted(() => {
  // 恢复出来的开合状态：先认「当前开着的那个」为最近打开的，再按最新几何落实
  if (store.batchOpen && !store.chatOpen) activePane.value = "batch";
  applyGeometry();
  if (typeof window !== "undefined") window.addEventListener("resize", onWindowResize);
  // 只观察「不由浮层决定尺寸」的两个元素：舞台与底部工具栏
  if (typeof ResizeObserver !== "undefined") {
    resizeObserver = new ResizeObserver(() => scheduleMeasure());
    if (stageEl.value) resizeObserver.observe(stageEl.value);
    toolbarEl = document.querySelector('[data-im="board-toolbar"]');
    if (toolbarEl) {
      toolbarObserver = new ResizeObserver(() => scheduleMeasure());
      toolbarObserver.observe(toolbarEl);
    }
  }
  // 舞台/工具栏都在首帧之后才量得准
  scheduleMeasure();
});

onBeforeUnmount(() => {
  if (typeof window !== "undefined") window.removeEventListener("resize", onWindowResize);
  if (measureTimer !== null) window.clearTimeout(measureTimer);
  measureTimer = null;
  resizeObserver?.disconnect();
  resizeObserver = null;
  toolbarObserver?.disconnect();
  toolbarObserver = null;
  // 摘掉自己写在舞台上的变量与标记（离开互动版不留残留）
  const stage = stageEl.value;
  if (stage) {
    for (const name of [
      "im-geo-mode",
      "--im-geo-chat-max-w",
      "--im-geo-chat-max-h",
      "--im-geo-batch-max-w",
      "--im-geo-batch-max-h",
      "--im-geo-batch-right",
      "--im-geo-gap",
      "--im-geo-chat-lift",
    ]) {
      if (name.startsWith("--")) stage.style.removeProperty(name);
      else stage.removeAttribute(name);
    }
  }
  appliedKey = "";
});

/** 入口只说明「这一批」与「其他批次还等着多少」，不把不同批次相加 */
function entryText(batch: IntentBatch): string {
  const other = groupIntentsByBatch(store.intents)
    .filter((item) => item.key !== batch.key)
    .reduce((sum, item) => sum + item.pendingIds.length, 0);
  return batchEntryText(batch, other);
}

function selectedFor(batch: IntentBatch): string[] {
  return selectedByBatch.value[batch.key] ?? [];
}

function setSelected(batch: IntentBatch, ids: string[]): void {
  selectedByBatch.value = { ...selectedByBatch.value, [batch.key]: ids };
}

function statusLabel(intent: Intent): string {
  return statusText(intent).label;
}

function approveSplit(batch: IntentBatch) {
  return splitBatchIn(store.intents, batch, selectedFor(batch), "approve");
}

function rejectSplit(batch: IntentBatch) {
  return splitBatchIn(store.intents, batch, selectedFor(batch), "reject");
}

function blockedReason(batch: IntentBatch, intentId: string): string {
  return approveSplit(batch).blocked.find((item) => item.id === intentId)?.reason ?? "";
}

function isPending(batch: IntentBatch, intentId: string): boolean {
  return batch.pendingIds.includes(intentId);
}

/** 这一批还剩多少没处理、已经处理掉多少（已处理项灰掉、不可再提交） */
function remainingCount(batch: IntentBatch): number {
  return batch.pendingIds.length;
}

function processedCount(batch: IntentBatch): number {
  return Math.max(0, batch.intentIds.length - batch.pendingIds.length);
}

function toggle(batch: IntentBatch, intentId: string): void {
  setSelected(batch, toggleBatchSelectionIn(batch, selectedFor(batch), intentId));
}

function selectAll(batch: IntentBatch): void {
  setSelected(batch, selectAllInBatch(batch));
}

function clearAll(batch: IntentBatch): void {
  setSelected(batch, clearBatchSelectionIn());
}

/** 点击条目：在板面上定位这项虚线预览（沿用 A 的 window 事件） */
function locate(intentId: string): void {
  const intent = store.intentById(intentId);
  locatePreview(intentId, intent ? previewBounds(intent.preview) : null);
}

async function decide(batch: IntentBatch, decision: "approve" | "reject"): Promise<void> {
  const split = decision === "approve" ? approveSplit(batch) : rejectSplit(batch);
  if (!split.ids.length) {
    notice.value = "没有可提交的项：未选中的继续等待审批。";
    return;
  }
  busy.value = true;
  error.value = null;
  try {
    const result = await store.decideBatch(
      decision === "approve" ? split.ids : [],
      decision === "reject" ? split.ids : [],
    );
    const failed = (result.results ?? []).filter((item) => !item.ok);
    notice.value =
      (decision === "approve" ? "批量批准" : "批量拒绝") +
      "：批准 " +
      result.approved.length +
      " 项，拒绝 " +
      result.rejected.length +
      " 项；未选中的继续等待。" +
      (failed.length
        ? "未成功 " +
          failed.length +
          " 项（" +
          failed.map((item) => item.detail ?? item.reason ?? "服务端未说明").join("；") +
          "）。"
        : "");
    clearAll(batch);
  } catch (err) {
    error.value = (err as Error).message;
  } finally {
    busy.value = false;
  }
}

/**
 * 只做清理：把已经不在这一批里的选择去掉，并保住阅读位置。
 * 不改变用户的选择、不做展开 / 收起，也不因为数量变化就重置选择。
 */
watch(
  batches,
  (list) => {
    const next: Record<string, string[]> = {};
    for (const batch of list) {
      const kept = pruneBatchSelection(batch, selectedByBatch.value[batch.key] ?? []);
      if (kept.length) next[batch.key] = kept;
    }
    selectedByBatch.value = next;
  },
  { deep: false },
);
</script>

<template>
  <!-- 根节点常驻：它同时是几何控制器的挂载点（父元素就是板面舞台） -->
  <div
    ref="trayEl"
    class="batch-tray"
    data-im="batch-area"
    :data-geo-mode="geometry?.mode ?? ''"
    :style="trayStyle"
    @keydown.stop="onKeydown"
    @keyup.stop
  >
    <!--
      空间不足（二选一）时的切换条：常驻、明确告知，不压在面板或工具栏上。
      位置由几何计划给出（面板底边已经让开这一行），内容与高度固定，所以不参与尺寸观察。
    -->
    <div
      v-if="switchBarVisible"
      class="overlay-switch"
      data-im="overlay-switch"
      role="group"
      aria-label="面板切换"
      :style="switchBarStyle"
    >
      <span class="switch-hint" data-im="overlay-switch-notice">空间不足，只展开一个面板（内容都还在）</span>
      <button
        class="switch-btn"
        type="button"
        data-im="overlay-switch-chat"
        :aria-pressed="store.chatOpen"
        :title="store.chatOpen ? '当前显示的是对话（消息与草稿都保留）' : '切到对话，消息与草稿都保留'"
        @click="showPane('chat')"
      >
        看对话
      </button>
      <button
        v-if="batches.length"
        class="switch-btn"
        type="button"
        data-im="overlay-switch-batch"
        :aria-pressed="store.batchOpen"
        :title="store.batchOpen ? '当前显示的是审批列表（勾选与阅读位置都保留）' : '切到审批列表，勾选与阅读位置都保留'"
        @click="showPane('batch')"
      >
        看审批列表
      </button>
    </div>

    <button
      v-for="batch in batches"
      :key="batch.key"
      class="entry qio-glass qio-glass--chip"
      type="button"
      data-im="batch-entry"
      :data-batch-key="batch.key"
      :aria-expanded="panelIsOpen"
      aria-controls="im-batch-list"
      @click="store.batchOpen = !store.batchOpen"
    >
      <span class="entry-title">批量审批</span>
      <span class="entry-count mono" :title="entryText(batch)">{{ entryText(batch) }}</span>
      <span class="entry-arrow" aria-hidden="true">{{ panelIsOpen ? "收起" : "展开" }}</span>
    </button>

    <section
      v-if="panelIsOpen"
      id="im-batch-list"
      ref="panelEl"
      class="panel"
      data-im="batch-list"
      role="dialog"
      aria-label="同一批待审批条目的批量处理"
      tabindex="-1"
      :style="panelStyle"
      @scroll.passive="onPanelScroll"
      @wheel.stop
    >
      <header class="panel-head">
        <h2 class="panel-title">同一批的待审批条目</h2>
        <button class="close" type="button" data-im="batch-close" @click="store.batchOpen = false">
          收起
        </button>
      </header>

      <p class="hint" role="note">
        只列同一批产生的条目（不同批次不合并）。勾选部分或全部后批量批准 / 拒绝；未选中的继续等待审批。
        已经处理过的条目会保留在下面并变灰，不能再被选中或提交。
      </p>


      <p v-if="notice" class="notice" role="status" data-im="batch-notice">{{ notice }}</p>
      <p v-if="error" class="error" role="alert" data-im="batch-error">批量操作失败：{{ error }}</p>

      <div v-for="batch in batches" :key="batch.key" class="batch-block" :data-batch-key="batch.key">
        <p class="batch-head mono" data-im="batch-remaining">
          剩余待处理 {{ remainingCount(batch) }} 项 · 已处理 {{ processedCount(batch) }} 项
        </p>
        <p class="summary">{{ batchSummaryIn(store.intents, batch, selectedFor(batch)) }}</p>

        <ul class="rows">
          <li
            v-for="intentId in batch.intentIds"
            :key="intentId"
            class="row"
            :class="{ 'row-done': !isPending(batch, intentId) }"
            data-im="batch-item"
            :data-intent-id="intentId"
            :data-im-state="isPending(batch, intentId) ? 'pending' : 'processed'"
          >
            <label v-if="isPending(batch, intentId)" class="pick">
              <input
                type="checkbox"
                :checked="selectedFor(batch).includes(intentId)"
                :data-intent-id="intentId"
                :disabled="busy"
                @change="toggle(batch, intentId)"
              />
              <span class="name">{{ store.intentById(intentId)?.title ?? intentId }}</span>
            </label>
            <span v-else class="pick processed">
              <span class="mark" aria-hidden="true">✓</span>
              <span class="name">{{ store.intentById(intentId)?.title ?? intentId }}</span>
              <span class="done-tag">已处理，不能再提交</span>
            </span>
            <span class="status">
              {{ store.intentById(intentId) ? statusLabel(store.intentById(intentId) as Intent) : "状态未知" }}
            </span>
            <button
              v-if="isPending(batch, intentId)"
              class="locate"
              type="button"
              data-im="batch-locate"
              :data-intent-id="intentId"
              @click="locate(intentId)"
            >
              定位预览
            </button>
            <p v-if="isPending(batch, intentId) && blockedReason(batch, intentId)" class="blocked">
              {{ blockedReason(batch, intentId) }}（仍可以批量拒绝）
            </p>
          </li>
        </ul>

        <div class="actions">
          <button
            class="btn primary"
            type="button"
            data-im="batch-approve"
            :data-batch-key="batch.key"
            :disabled="busy || !approveSplit(batch).ids.length"
            @click="decide(batch, 'approve')"
          >
            批量批准（{{ approveSplit(batch).ids.length }}）
          </button>
          <button
            class="btn"
            type="button"
            data-im="batch-reject"
            :data-batch-key="batch.key"
            :disabled="busy || !rejectSplit(batch).ids.length"
            @click="decide(batch, 'reject')"
          >
            批量拒绝（{{ rejectSplit(batch).ids.length }}）
          </button>
          <button class="btn ghost" type="button" data-im="batch-all" @click="selectAll(batch)">
            全选
          </button>
          <button class="btn ghost" type="button" data-im="batch-clear" @click="clearAll(batch)">
            清空选择
          </button>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* 位置由几何计划决定（right / max-width 走内联），这里只留外观 */
.batch-tray {
  position: absolute;
  right: var(--sp-4);
  top: var(--sp-4);
  z-index: var(--im-z-batch, 40);
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--sp-2);
}
.entry {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  /* 入口是一个胶囊：窄窗口里宁可省略文案，也不许换行撑高、更不许把文字溢出到聊天面板上 */
  max-width: 100%;
  min-width: 0;
  font: inherit;
  color: var(--text-primary);
  border-radius: var(--r-pill);
  padding: var(--sp-1) var(--sp-4);
  cursor: pointer;
}
.entry:hover { border-color: var(--accent); }
.entry-title {
  flex: none;
  font-family: var(--serif);
  font-size: var(--fs-sm);
  color: var(--text-strong);
}
.entry-count {
  flex: 1 1 auto;
  min-width: 0;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.entry-arrow { flex: none; font-size: var(--fs-xs); color: var(--link); }
.panel {
  width: 100%;
  overflow: auto;
  overscroll-behavior: contain;
  padding: var(--sp-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
  box-shadow: var(--elev-floating, var(--shadow-2));
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.panel:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.panel-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
}
.panel-title {
  margin: 0;
  flex: 1;
  font-family: var(--serif);
  font-size: var(--fs-sm);
  color: var(--text-strong);
}
.close {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
}
.hint,
.notice,
.error,
.summary,
.blocked,
.batch-head,
.switch-notice {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
  color: var(--text-muted);
}
.hint { color: var(--text-faint); }
.notice { color: var(--text-secondary); }
.error { color: var(--danger); }
.switch-notice { color: var(--warning); }

/*
  面板切换条（契约 §11.7 / §11.8）：定位与高度**全部**来自几何计划写进行内样式的矩形
  （position: fixed + 视口坐标），这里只做外观：边框、底色、圆角、字号、选中态、hover、focus。
  全部走既有令牌，明暗两主题自动成立；高度由行内样式定成常量、box-sizing: border-box，
  内容只允许在一条线上收缩（提示文字省略），所以它不会把自己撑高、也不会参与尺寸观察。
*/
.overlay-switch {
  position: fixed;
  z-index: var(--im-z-batch, 40);
  box-sizing: border-box;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: 0 var(--sp-2);
  overflow: hidden;
  white-space: nowrap;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-1);
  color: var(--text-primary);
  font-family: var(--sans);
}
.switch-hint {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}
.switch-btn {
  flex: none;
  font: inherit;
  font-size: var(--fs-xs);
  line-height: 1;
  min-height: 26px;
  color: var(--text-primary);
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  padding: var(--sp-1) var(--sp-3);
  cursor: pointer;
}
.switch-btn:hover {
  color: var(--text-strong);
  border-color: var(--accent);
}
/* 当前正在显示的那一个：用强调色说清「现在看的是它」，同时仍然可点（不会变成点不动的死按钮） */
.switch-btn[aria-pressed="true"] {
  color: var(--on-accent);
  background: var(--accent);
  border-color: var(--accent);
}
.switch-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
@media (prefers-reduced-motion: reduce) {
  .switch-btn { transition: none; }
}
.batch-block {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  border-top: 1px solid var(--border-subtle);
  padding-top: var(--sp-2);
}
.batch-head { color: var(--text-secondary); }
.rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.row {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  flex-wrap: wrap;
  padding-bottom: var(--sp-1);
  border-bottom: 1px solid var(--border-subtle);
}
/* 已处理：只降调，不消失（用户要看得见「处理到哪了」） */
.row-done .name,
.row-done .status { color: var(--text-faint); }
.row-done .processed { cursor: default; }
.pick {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex: 1;
  min-width: 0;
  cursor: pointer;
}
.processed { cursor: default; }
.mark { font-family: var(--mono); font-size: var(--fs-xs); color: var(--success); }
.done-tag { font-size: var(--fs-xs); color: var(--text-faint); white-space: nowrap; }
.name {
  font-size: var(--fs-sm);
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.status { font-size: var(--fs-xs); color: var(--text-muted); white-space: nowrap; }
.locate {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
}
.locate:hover { color: var(--accent-hover); }
.blocked { flex-basis: 100%; color: var(--danger); padding-left: var(--sp-4); }
.actions {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
  padding-top: var(--sp-1);
}
.btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-3);
  cursor: pointer;
}
.btn.primary {
  color: var(--on-accent);
  background: var(--accent);
  border-color: var(--accent);
}
.btn.primary:hover:enabled { background: var(--accent-hover); }
.btn.ghost { background: none; color: var(--link); }
.btn:disabled { opacity: 0.55; cursor: default; }
.entry:focus-visible,
.btn:focus-visible,
.close:focus-visible,
.locate:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }

@media (max-width: 900px) {
  .batch-tray { right: var(--sp-2); top: var(--sp-2); }
}
</style>
