<script setup lang="ts">
import { vQioTip } from "../composables/useQioTip";
/**
 * 对话页 v2 外壳（A 负责外层布局与顶部全局栏入口；中央/右侧内容由 B/C 各自组件承担）。
 *
 * 布局：
 *   顶部全局栏（WindowChrome，A）—— QIO 字标 / 设置 / 窗口按钮
 *   ├─ 左侧任务栏（A）        ← 可拖宽、可收起；窄窗口临时收起（不写偏好）
 *   ├─ 中央对话工作区（B）
 *   └─ 右侧只读预览（C）      ← 可拖宽、可收起、可全窗口
 *
 * 页面切换（对话/互动/设置）不初始化业务状态：shell.load() 与 loadHistory() 只在
 * 第一次进入时执行，返回时最多做一次幂等刷新；草稿/附件/阅读位置各自在 store 里保留。
 * 右下角记忆星球入口保持现有 PlanetDock（用户明确：样式与功能不改）。
 */
import {
  computed,
  defineAsyncComponent,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";
import { useSessionStore } from "../stores/session";
import { useConversationShellStore } from "../stores/conversationShell";
import { useConversationTasksStore } from "../stores/conversationTasks";
import {
  LEFT_DEFAULT,
  LEFT_MAX,
  LEFT_MIN,
  RIGHT_DEFAULT,
  RIGHT_MAX,
  RIGHT_MIN,
} from "../conversation/layoutPrefs";
import { useEscapeLayer } from "../composables/useEscapeLayer";
import { listenFocusTask } from "../services/conversationTasks";
import {
  registerNoticeNavigationHost,
  unregisterNoticeNavigationHost,
  type NoticeNavigationHost,
} from "../services/noticeNavigation";
import TaskRail from "../components/conversation/TaskRail.vue";
import ChatColumn from "../components/conversation/ChatColumn.vue";
import PreviewPanel from "../components/conversation/PreviewPanel.vue";
import UnifiedApprovalLayer from "../components/conversation/UnifiedApprovalLayer.vue";
import PlanetDock from "../components/PlanetDock.vue";
import PlanetBoot from "../components/PlanetBoot.vue";
import RecoveryInbox from "../components/RecoveryInbox.vue";
import { useConversationPreviewStore } from "../stores/conversationPreview";
import {
  registerConversationPreviewOpener,
  registerConversationReplyCodeOpener,
} from "../components/conversation/previewBridge";
import { registerExecutionTaskLocator } from "../components/conversation/taskLocator";
import type { MemoryOpenTarget } from "../stores/memoryQuestions";

const PlanetView = defineAsyncComponent({
  loader: () => import("./PlanetView.vue"),
  loadingComponent: PlanetBoot,
  delay: 0,
});

const session = useSessionStore();
const shell = useConversationShellStore();
const tasks = useConversationTasksStore();
const preview = useConversationPreviewStore();

const planetOpen = ref(false);
const planetMounted = ref(false);
const planetSeq = ref(0);
/** 打开星球时定位到哪个页签 / 哪条待确认问题（记忆问题提醒 → 星球待确认）。 */
const planetTab = ref<"topic" | "knowledge" | "entity" | "memory">("topic");
const planetFocusPendingId = ref<string | null>(null);
/** 「未完成事项」恢复区：左栏任务的「去处理」会打开它（原有恢复动作都在这里）。 */
const recoveryOpen = ref(false);
/** 本次打开恢复窗口要滚动并聚焦的记录（来自「去处理」事件；null = 不指定）。 */
const recoveryFocusRecordId = ref<string | null>(null);
/** 收件箱暴露 handleEscape()：Esc 先收它的内层详情，未消费才关弹窗（契约 §4.2）。 */
const recoveryInboxRef = ref<unknown>(null);
/** 各模态层打开前的焦点：关闭后归还给触发元素（#38/#51/#79）。 */
let planetReturnFocus: HTMLElement | null = null;
let previewReturnFocus: HTMLElement | null = null;
let recoveryReturnFocus: HTMLElement | null = null;
let stopViewport: (() => void) | null = null;
let stopFocusListener: (() => void) | null = null;
let stopPreviewIdentityWatch: (() => void) | null = null;

/** 记住当前真实焦点元素（body / documentElement 不算，避免把「无焦点」当触发点）。 */
function rememberFocus(): HTMLElement | null {
  if (typeof document === "undefined") return null;
  const active = document.activeElement as HTMLElement | null;
  if (!active || active === document.body || active === document.documentElement) return null;
  return active;
}

/**
 * 把焦点还给触发元素；触发元素已不在 DOM（或落在仍被 inert 隔离的背景里）时用 fallback。
 * 一律等一次 nextTick：先让覆盖层卸载、inert 解除，再聚焦，避免浏览器把焦点丢回 body。
 */
function restoreFocus(target: HTMLElement | null, fallback?: () => HTMLElement | null): void {
  if (typeof document === "undefined") return;
  void nextTick(() => {
    const fallbackEl = target && target.isConnected ? null : (fallback?.() ?? null);
    const el = target && target.isConnected ? target : fallbackEl;
    if (!el || !el.isConnected || el.closest("[inert]")) return;
    el.focus({ preventScroll: true });
  });
}

/** 预览层收起后把焦点放在哪：分栏回到当前标签，收起状态回到预览恢复入口。 */
function previewFallbackFocus(collapsed: boolean): HTMLElement | null {
  if (typeof document === "undefined") return null;
  if (collapsed) return document.querySelector<HTMLElement>(".preview-reopen");
  return (
    document.querySelector<HTMLElement>('[data-preview-panel] [role="tab"][aria-selected="true"]') ??
    document.querySelector<HTMLElement>("[data-preview-panel]")
  );
}

// 接线桥必须在**本组件 setup 期间**注册：消息流/任务栏子组件在 setup 时读取
// 「有没有接线」决定是否渲染按钮（放到 onMounted 会晚于子组件 setup，按钮永远不出现）。
registerExecutionTaskLocator((ref) => {
  void tasks.focusTask(`${ref.source}:${ref.sourceId}`);
  return true;
});
registerConversationPreviewOpener((target) => {
  void preview.openPreview(target);
});
registerConversationReplyCodeOpener((input) => {
  void preview.openReplyCode(input);
});

// ── 三栏拖动 ───────────────────────────────────────────────────────────────

type DragKind = "left" | "right" | null;
const dragKind = ref<DragKind>(null);
let dragStartX = 0;
let dragStartWidth = 0;

function setDraggingChrome(on: boolean) {
  if (typeof document === "undefined") return;
  document.body.style.cursor = on ? "col-resize" : "";
  document.body.style.userSelect = on ? "none" : "";
}

function beginDrag(kind: Exclude<DragKind, null>, event: PointerEvent) {
  dragKind.value = kind;
  dragStartX = event.clientX;
  // 从「当前实际看到的宽度」开始拖：窗口被压缩时用户拖的是眼前这条边，
  // 不是早已放不下的保存值（#83）。
  dragStartWidth = kind === "left" ? shell.leftWidth : shell.rightWidth;
  (event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId);
  setDraggingChrome(true);
  event.preventDefault();
}

function moveDrag(event: PointerEvent) {
  if (!dragKind.value) return;
  const delta = event.clientX - dragStartX;
  // 拖动过程中只改内存：松手才落盘，避免每移动一像素写一次服务端。
  // 保存的是用户想要的宽度（只夹到可调范围），窗口太窄时显示侧再收缩，不改写偏好。
  if (dragKind.value === "left") {
    shell.prefs.leftWidth = Math.round(dragStartWidth + delta);
    shell.prefs.leftCollapsed = false;
  } else {
    shell.prefs.rightWidth = Math.round(dragStartWidth - delta);
  }
}

function endDrag() {
  if (!dragKind.value) return;
  const kind = dragKind.value;
  dragKind.value = null;
  setDraggingChrome(false);
  // 松手才持久化；用户手动调整的值优先于默认值。
  if (kind === "left") shell.setLeftWidth(shell.prefs.leftWidth);
  else shell.setRightWidth(shell.prefs.rightWidth);
}

/** 双击分隔条恢复该栏默认宽度（并持久化）。 */
function resetWidth(kind: "left" | "right") {
  if (kind === "left") shell.setLeftWidth(LEFT_DEFAULT);
  else shell.setRightWidth(RIGHT_DEFAULT);
}

function nudgeSplitter(kind: "left" | "right", event: KeyboardEvent) {
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  const step = event.shiftKey ? 32 : 12;
  event.preventDefault();
  const direction = event.key === "ArrowRight" ? 1 : -1;
  // 方向键同样从实际宽度起算：窗口压缩时不会出现「按了没反应」。
  if (kind === "left") shell.setLeftWidth(shell.leftWidth + direction * step);
  else shell.setRightWidth(shell.rightWidth - direction * step);
}

// ── 收起后的任务入口 / 窄窗口抽屉 ─────────────────────────────────────────

const railDrawer = ref(false);
/** 这次打开抽屉是否要自动把焦点放进第一个按钮（用户点击时是；通知导航不抢焦点）。 */
const railDrawerAutofocus = ref(true);
const railDrawerEl = ref<HTMLElement | null>(null);
const railEntryEl = ref<HTMLButtonElement | null>(null);

const railBadge = computed(() => {
  const count = tasks.badgeCount;
  return count > 0 ? String(count) : "";
});
const railBadgeTone = computed(() => (tasks.attentionCount > 0 ? "attention" : "active"));
const railEntryLabel = computed(() => {
  const base = shell.autoCollapsedLeft ? "打开任务列表（窗口较窄，任务栏暂时收起）" : "展开任务栏";
  const parts = [base];
  if (tasks.attentionCount > 0) parts.push(tasks.attentionCount + " 项需要处理");
  else if (tasks.activeCount > 0) parts.push(tasks.activeCount + " 项运行或排队");
  return parts.join("，");
});

function onRailEntry() {
  // 窗口窄到放不下（临时收起）：抽屉式临时展开，不改写用户偏好；
  // 宽窗口只是用户手动收起：直接展开并记住选择。
  if (shell.autoCollapsedLeft) {
    railDrawerAutofocus.value = true;
    railDrawer.value = true;
  } else shell.toggleLeft();
}

async function closeRailDrawer() {
  if (!railDrawer.value) return;
  railDrawer.value = false;
  await nextTick();
  railEntryEl.value?.focus();
}

// 手动收起/变宽后任务栏恢复可见：抽屉不再需要。
watch(
  () => shell.leftVisible,
  (visible) => {
    if (visible && railDrawer.value) railDrawer.value = false;
  },
);

// 中央执行记录要求定位任务时，如果左栏正被（手动或临时）收起，就临时展开抽屉。
watch(
  () => tasks.focusTarget,
  (focus) => {
    if (focus && !shell.leftVisible) railDrawer.value = true;
  },
);

watch(railDrawer, async (open) => {
  if (!open) return;
  // 通知导航打开的抽屉保持当前焦点：顶部短通知「不抢焦点」的既有规则不能在这里破例。
  if (!railDrawerAutofocus.value) {
    railDrawerAutofocus.value = true;
    return;
  }
  await nextTick();
  const root = railDrawerEl.value;
  if (!root) return;
  const first = root.querySelector<HTMLElement>("button:not(:disabled)");
  first?.focus();
});

// ── 通知点击 → 任务列表可见落点（供 services/noticeNavigation 调用） ────────

/**
 * 让任务列表真实可见：关闭盖住它的页内覆盖层，必要时展开左栏 / 窄窗抽屉。
 * 只调整可见性与层级，不处理任何任务，也不清空任何草稿或阅读位置。
 */
async function ensureTaskListVisibleForNotice(): Promise<boolean> {
  // 1) 页内覆盖层：记忆星球 / 恢复收件箱；全窗口预览也会整块盖住三栏。
  //    这是程序化导航，不还原焦点、也不清除用户标签与阅读位置。
  if (recoveryOpen.value) {
    recoveryOpen.value = false;
    recoveryFocusRecordId.value = null;
    recoveryReturnFocus = null;
  }
  if (planetOpen.value) {
    planetOpen.value = false;
    planetReturnFocus = null;
  }
  if (shell.effectivePreviewFull) {
    // 手动全窗口回分栏；窄窗临时全窗口没有分栏可回，直接收起预览。
    if (shell.prefs.previewFull) shell.setPreviewFull(false);
    else shell.setRightOpen(false);
  }
  // 2) 左栏：宽窗按现有开关展开（用户手动收起时）；窄窗走现有任务抽屉。
  if (!shell.leftVisible) {
    if (shell.prefs.leftCollapsed) shell.toggleLeft();
    await nextTick();
    if (!shell.leftVisible) {
      railDrawerAutofocus.value = false;
      railDrawer.value = true;
    }
  }
  await nextTick();
  return shell.leftVisible || railDrawer.value;
}

const noticeNavigationHost: NoticeNavigationHost = {
  ensureTaskListVisible: ensureTaskListVisibleForNotice,
};
// 本页拥有星球 / 恢复区 / 抽屉这些页面级可见性状态，所以由本页注册可见性宿主。
registerNoticeNavigationHost(noticeNavigationHost);

// ── 星球入口（保持现有行为） ───────────────────────────────────────────────

function mountPlanetWhenIdle() {
  const start = () => {
    if (planetMounted.value) return;
    if (session.turnRunning) {
      window.setTimeout(start, 1500);
      return;
    }
    planetMounted.value = true;
  };
  const ric = (window as unknown as { requestIdleCallback?: (cb: () => void, o?: { timeout: number }) => number })
    .requestIdleCallback;
  if (typeof ric === "function") ric(start, { timeout: 4000 });
  else window.setTimeout(start, 2000);
}

function openPlanet() {
  planetReturnFocus = rememberFocus();
  planetSeq.value += 1;
  planetMounted.value = true;
  planetOpen.value = true;
}

function closePlanet(seq?: number) {
  if (seq !== undefined && seq !== planetSeq.value) return;
  planetOpen.value = false;
  // #38：退出把焦点还给星球入口（契约 §4.3 的 [data-planet-entry]）。
  restoreFocus(planetReturnFocus, () =>
    typeof document === "undefined"
      ? null
      : document.querySelector<HTMLElement>("[data-planet-entry]"),
  );
  planetReturnFocus = null;
}

const previewOverlayOpen = computed(() => shell.rightVisible && shell.effectivePreviewFull);

/**
 * 页面级模态层背后的对话必须 inert：Tab / Enter / 鼠标都到不了下层
 * （#38 星球、#51 全窗口预览、#79 恢复窗口）。分栏预览不做隔离 —— 两边都要能操作。
 */
const backgroundInert = computed(
  () => planetOpen.value || previewOverlayOpen.value || recoveryOpen.value || railDrawer.value,
);

/** 分栏 → 全窗口（用户手动）：记住触发它的标签/按钮，收起时归还（#51）。 */
function enterPreviewFull(): void {
  previewReturnFocus = rememberFocus();
  shell.setPreviewFull(true);
}

/**
 * 全窗口预览 → 返回对话（#50）：
 * - 大窗口手动全窗口：真正回到分栏，标签/搜索/阅读位置都在；
 * - 小窗口临时全窗口：没有分栏可回，收起预览本身（不写偏好），标签/位置仍保留。
 */
function exitPreviewFull(): void {
  const temporary = !shell.prefs.previewFull;
  const returnTarget = previewReturnFocus;
  previewReturnFocus = null;
  if (temporary) {
    shell.setRightOpen(false);
    restoreFocus(returnTarget, () => previewFallbackFocus(true));
    return;
  }
  shell.setPreviewFull(false);
  restoreFocus(returnTarget, () => previewFallbackFocus(false));
}

function onPreviewToggleFull(): void {
  if (!shell.effectivePreviewFull) {
    enterPreviewFull();
    return;
  }
  exitPreviewFull();
}

/** 收起预览：全窗口时焦点回预览入口，分栏时同样给恢复入口（不可丢到 body）。 */
function onPreviewClose(): void {
  const returnTarget = previewReturnFocus ?? rememberFocus();
  previewReturnFocus = null;
  shell.setRightOpen(false);
  restoreFocus(returnTarget, () => previewFallbackFocus(true));
}

// Esc 走共用层级栈：#79 恢复窗口最靠前（先收详情再关弹窗），其次任务抽屉，最后全窗口预览。
// 星球若自己注册了层级（navigation 的 PlanetView），它后注册、先处理，这里不抢。
useEscapeLayer({
  id: "conversation-page",
  active: () => recoveryOpen.value || railDrawer.value || previewOverlayOpen.value,
  onEscape: () => {
    if (recoveryOpen.value) {
      const inbox = recoveryInboxRef.value as { handleEscape?: () => boolean } | null;
      const consumed = inbox?.handleEscape?.() === true;
      if (!consumed) closeRecovery();
      return true;
    }
    if (railDrawer.value) {
      void closeRailDrawer();
      return true;
    }
    if (previewOverlayOpen.value && !planetOpen.value) {
      exitPreviewFull();
      return true;
    }
    return false;
  },
});

/** 抽屉是模态层：Tab 在抽屉内循环，不跑到背后的页面。 */
function trapDrawerFocus(event: KeyboardEvent) {
  const root = railDrawerEl.value;
  if (!root) return;
  const focusables = Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not(:disabled), [href], input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])',
    ),
  );
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const active = document.activeElement as HTMLElement | null;
  const inside = Boolean(active && root.contains(active));
  if (event.shiftKey) {
    if (!inside || active === first) {
      event.preventDefault();
      last.focus();
    }
    return;
  }
  if (!inside || active === last) {
    event.preventDefault();
    first.focus();
  }
}

// 窗口重新可见（休眠唤醒 / 切回桌面）时补一次幂等刷新。
function onVisibilityChange() {
  if (typeof document === "undefined" || document.visibilityState !== "visible") return;
  if (tasks.current.loaded) void tasks.refresh("current", { silent: true });
  if (shell.prefs.taskTab === "history" && tasks.history.loaded) {
    void tasks.refresh("history", { silent: true });
  }
}

// ── 恢复事项（原有恢复动作集中在这里，不复制一套） ─────────────────────────

function openRecovery(recordId: string | null = null): void {
  if (!recoveryOpen.value) recoveryReturnFocus = rememberFocus();
  recoveryFocusRecordId.value = recordId;
  recoveryOpen.value = true;
}

/**
 * 任务栏「去处理」/ 恢复通知（契约 §4.1）：
 * 事件 detail.recordId 指明要聚焦的那一条；没有就打开清单本身。
 */
function onOpenRecoveryEvent(event: Event): void {
  const detail = (event as CustomEvent<{ recordId?: unknown }>).detail;
  const recordId = typeof detail?.recordId === "string" && detail.recordId ? detail.recordId : null;
  openRecovery(recordId);
}

function closeRecovery(): void {
  recoveryOpen.value = false;
  recoveryFocusRecordId.value = null;
  const target = recoveryReturnFocus;
  recoveryReturnFocus = null;
  // #79：关闭后归还焦点（触发「去处理」的按钮还在就还给它，否则给任务入口）。
  restoreFocus(target, () =>
    typeof document === "undefined"
      ? null
      : document.querySelector<HTMLElement>("[data-rail-entry]"),
  );
}

// ── 记忆问题提醒 → 星球待确认定位（不改星球入口样式/功能） ────────────────

function onOpenMemory(target: MemoryOpenTarget): void {
  planetTab.value = target.tab === "memory" ? "memory" : "topic";
  planetFocusPendingId.value = target.itemId ?? null;
  openPlanet();
}

/**
 * 右侧预览开/关会改变「右下角是否被固定面板占据」：让星球入口重新计算一次位置
 * （useFloatingWindow 在 resize 时会避开 [data-preview-panel] 等固定控件）。
 * 只触发布局重算，不改星球入口样式或拖动行为。
 */
watch(
  () => shell.rightVisible,
  () => {
    if (typeof window === "undefined") return;
    nextTick(() => window.dispatchEvent(new Event("resize")));
  },
);

// ── 生命周期（页面切换不重复初始化） ──────────────────────────────────────

onMounted(() => {
  /**
   * 重新挂载时恢复预览运行时（与卸载时的 preview.dispose() 成对）。
   *
   * dispose() 会把 store 的「已卸载」闸门置 true 并中止在途请求；以前没有任何地方
   * 复位，从设置页返回对话后重新打开同一段代码，ensureLoaded 直接 return，
   * 阅读器永远停在「正在读取这个文件…」（只有整页刷新才恢复）。
   * 这里只恢复「能接收新请求」，标签与阅读位置一直在 shell 里，不受影响。
   */
  preview.resume();
  // 只有第一次进入才读布局偏好与历史；从设置/互动页返回时不重置现有状态。
  if (!shell.loaded) void shell.load();
  stopViewport = shell.installViewportTracking();
  if (session.history.status === "idle") void session.loadHistory();
  // 互动页确认 / 恢复 / 重试之后留下的「历史可能过期」：回到对话页时兑现一次（只读，不轮询）
  else void session.reloadHistoryIfStale();

  // 事件订阅 / 轮询由应用级生命周期（GlobalFeedbackHost → acquireGlobal）负责：
  // 对话页只保留自己的幂等刷新，卸载页面不再停掉全局任务数据。
  if (tasks.current.loaded) {
    // 幂等刷新：离开期间可能出现新任务/新状态，但不清空、不重置展开与滚动。
    void tasks.refresh("current", { silent: true });
    if (shell.prefs.taskTab === "history" && tasks.history.loaded) {
      void tasks.refresh("history", { silent: true });
    }
  } else {
    void tasks.ensureLoaded();
  }
  stopFocusListener = listenFocusTask((taskId) => {
    void tasks.focusTask(taskId);
  });
  /**
   * 回复代码标签的身份交接：刚生成的回复用本机乐观 id（`local_…`），历史快照接手后
   * 同一条消息换成后端 id。不交接的话，同一段代码会变成两个标签（旧标签靠内存正文
   * 还能读，再点一次却新开一个空标签）。只在能确定是同一条消息时迁移。
   */
  stopPreviewIdentityWatch = watch(
    /**
     * 键里同时带 id 与轮次身份：临时消息的 id 不变、但轮次身份是后来才补上的
     * （TURN_START 之后才拿到 turnId）时，也要重新观察一次 —— 否则交接永远等不到身份。
     */
    () => session.messages.map((m) => m.id + ":" + String(m.turnId ?? "")).join("\u0000"),
    () => {
      preview.reconcileReplyCodeIdentities(session.messages);
    },
    { immediate: true },
  );
  window.addEventListener("qio:open-recovery", onOpenRecoveryEvent);

  mountPlanetWhenIdle();
  document.addEventListener("visibilitychange", onVisibilityChange);
});

onBeforeUnmount(() => {
  stopViewport?.();
  stopViewport = null;
  stopFocusListener?.();
  stopFocusListener = null;
  stopPreviewIdentityWatch?.();
  stopPreviewIdentityWatch = null;
  document.removeEventListener("visibilitychange", onVisibilityChange);
  window.removeEventListener("qio:open-recovery", onOpenRecoveryEvent);
  registerExecutionTaskLocator(null);
  registerConversationPreviewOpener(null);
  registerConversationReplyCodeOpener(null);
  setDraggingChrome(false);
  unregisterNoticeNavigationHost(noticeNavigationHost);
  // 页面卸载不停全局任务数据（引用计数归零由应用级宿主负责）；只收本页自己的预览状态。
  preview.dispose();
  // 最后一次布局改动要落盘（防抖窗口内切走页面时不能丢）。
  void shell.flush();
});
</script>

<template>
  <div
    class="conversation-workspace"
    :class="{
      'left-collapsed': !shell.leftVisible,
      'preview-full': shell.effectivePreviewFull,
    }"
    :data-dragging="dragKind ?? undefined"
    :data-left-auto="shell.autoCollapsedLeft ? 'true' : 'false'"
    :data-center-width="shell.centerWidth"
    :data-preview-full="shell.effectivePreviewFull ? 'true' : 'false'"
  >
    <div class="workspace-row" :inert="backgroundInert ? true : undefined">
      <TaskRail
        v-if="shell.leftVisible"
        class="left-pane"
        :style="{ width: shell.leftWidth + 'px' }"
      />

      <div
        v-if="shell.leftVisible"
        class="splitter"
        role="separator"
        aria-orientation="vertical"
        aria-label="调整左侧任务栏宽度"
        :aria-valuemin="LEFT_MIN"
        :aria-valuemax="LEFT_MAX"
        :aria-valuenow="shell.leftWidth"
        :aria-valuetext="shell.leftWidth + ' 像素'"
        tabindex="0"
        @pointerdown="beginDrag('left', $event)"
        @pointermove="moveDrag"
        @pointerup="endDrag"
        @pointercancel="endDrag"
        @keydown="nudgeSplitter('left', $event)"
        @dblclick="resetWidth('left')"
      ></div>

      <div v-else class="collapsed-rail" aria-label="任务栏已收起">
        <button
          ref="railEntryEl"
          class="rail-btn"
          type="button"
          data-rail-entry
          v-qio-tip="railEntryLabel"
          :aria-label="railEntryLabel"
          :aria-expanded="railDrawer ? 'true' : 'false'"
          @click="onRailEntry"
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <rect x="2.5" y="3" width="11" height="10" rx="1.5" />
            <path d="M6.5 3v10M9 6h2M9 10h2" />
          </svg>
          <span class="rail-label">任务</span>
          <span
            v-if="railBadge"
            class="rail-badge mono"
            :data-tone="railBadgeTone"
            aria-hidden="true"
          >{{ railBadge }}</span>
        </button>
      </div>

      <ChatColumn class="center-pane">
        <template #planet>
          <PlanetDock anchored @open="openPlanet" />
        </template>
      </ChatColumn>

      <div
        v-if="shell.splitRightVisible"
        class="splitter"
        role="separator"
        aria-orientation="vertical"
        aria-label="调整文件预览宽度"
        :aria-valuemin="RIGHT_MIN"
        :aria-valuemax="RIGHT_MAX"
        :aria-valuenow="shell.rightWidth"
        :aria-valuetext="shell.rightWidth + ' 像素'"
        tabindex="0"
        @pointerdown="beginDrag('right', $event)"
        @pointermove="moveDrag"
        @pointerup="endDrag"
        @pointercancel="endDrag"
        @keydown="nudgeSplitter('right', $event)"
        @dblclick="resetWidth('right')"
      ></div>

      <PreviewPanel
        v-if="shell.splitRightVisible"
        class="right-pane"
        :style="{ width: shell.rightWidth + 'px' }"
        @close="onPreviewClose"
        @toggle-full="onPreviewToggleFull"
      />
    </div>

    <!-- 全窗口预览：外层负责卡片网格的内缩/圆角，内层只填满它（背景由 inert 隔离） -->
    <div
      v-if="previewOverlayOpen"
      class="preview-overlay"
      :inert="planetOpen || recoveryOpen ? true : undefined"
    >
      <PreviewPanel full @close="onPreviewClose" @toggle-full="onPreviewToggleFull" />
    </div>

    <!-- 任务栏被窗口宽度临时收起时的抽屉（不改写偏好；Esc / 点外部关闭） -->
    <div
      v-if="railDrawer"
      class="rail-drawer-layer"
      :inert="planetOpen || previewOverlayOpen ? true : undefined"
    >
      <div class="rail-drawer-backdrop" aria-hidden="true" @click="closeRailDrawer"></div>
      <div
        ref="railDrawerEl"
        class="rail-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="任务列表"
        :style="{ width: shell.prefs.leftWidth + 'px' }"
      >
        <TaskRail drawer @close="closeRailDrawer" />
      </div>
    </div>

    <!-- 预览栏收起但仍有标签：贴边小入口恢复（与右下角星球入口错开：在中线偏上） -->
    <button
      v-if="!shell.rightVisible && shell.prefs.previewTabs.length"
      class="preview-reopen"
      type="button"
      v-qio-tip="'打开文件预览（' + shell.prefs.previewTabs.length + ' 个标签）'"
      :aria-label="'打开文件预览，' + shell.prefs.previewTabs.length + ' 个标签'"
      @click="shell.setRightOpen(true)"
    >
      <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M10 4.5 5.5 8 10 11.5" /></svg>
      <span class="preview-reopen-count mono">{{ shell.prefs.previewTabs.length }}</span>
    </button>

    <!-- 未完成事项（恢复区）：原有继续 / 重发 / 忽略 / 重试动作都在这里，
         左栏任务的「去处理」把它打开；不另做一套恢复逻辑。 -->
    <div v-if="recoveryOpen" class="recovery-layer" :inert="planetOpen ? true : undefined">
      <div class="recovery-backdrop" aria-hidden="true" @click="closeRecovery"></div>
      <section
        class="recovery-panel"
        role="dialog"
        aria-modal="true"
        aria-label="未完成事项"
      >
        <!-- 文案 5/6：标题（含条数）与后果说明只由收件箱给一份；页面不再重复标题与通用导语。 -->
        <header class="recovery-head">
          <button class="qio-btn quiet mini" type="button" @click="closeRecovery">关闭</button>
        </header>
        <!-- focus-record-id 是契约 §4.2 的冻结接口（基线的 RecoveryInbox 尚未声明该 prop，
             用 v-bind 透传，合并 recovery 分支后即为正式 prop） -->
        <RecoveryInbox
          ref="recoveryInboxRef"
          v-bind="{ 'focus-record-id': recoveryFocusRecordId ?? undefined }"
        />
      </section>
    </div>

    <UnifiedApprovalLayer @open-memory="onOpenMemory" />
    <PlanetView
      v-if="planetMounted"
      :seq="planetSeq"
      :open="planetOpen"
      :initial-tab="planetTab"
      :focus-pending-id="planetFocusPendingId"
      @close="closePlanet"
    />
  </div>
</template>

<style scoped>
.conversation-workspace {
  /* 卡片之间唯一间距来源：顶栏→卡片、左栏→话题、话题→消息、左右预览都用它 */
  --conversation-card-gap: 12px;
  position: relative;
  isolation: isolate;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
  /* 背景改由整窗 ::before 承担：从窗口 y=0 开始，而不是从 workspace 的 y=80 开始 */
  background: transparent;
  color: var(--text-primary);
  font-family: var(--sans);
  animation: workspace-in var(--dur-page-out) var(--ease-out) both;
}
@keyframes workspace-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
/* 整窗背景层：fixed 到视口，覆盖全局栏四周；全局栏/卡片仍是表面色，不被染色 */
.conversation-workspace::before {
  content: "";
  position: fixed;
  /* 背景层只画在可见内容区（含圆角），不再把透明留白带染色 */
  inset: var(--window-frame-inset);
  border-radius: var(--window-radius);
  z-index: -1;
  pointer-events: none;
  background:
    radial-gradient(ellipse at 0% 0%, var(--accent-soft) 0, transparent 46%),
    var(--bg-base);
}
.workspace-row {
  display: flex;
  flex: 1;
  min-height: 0;
  min-width: 0;
  /* 顶栏下沿 y=70 → 卡片顶 y=82（12px 间距）；左右/底边保持 8px 外沿 */
  padding: 2px 8px 8px;
  gap: 0;
  overflow: hidden;
}
.left-pane {
  flex: none;
  min-width: 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-surface);
  box-shadow: var(--shadow-1);
  overflow: hidden;
}
.center-pane {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}
.right-pane {
  flex: none;
  min-width: 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-surface);
  box-shadow: var(--shadow-1);
  overflow: hidden;
}
.splitter {
  flex: none;
  width: var(--conversation-card-gap, 12px);
  position: relative;
  cursor: col-resize;
  touch-action: none;
}
.splitter::after {
  position: absolute;
  content: "";
  width: 2px;
  top: 42%;
  height: 40px;
  left: calc(50% - 1px);
  border-radius: var(--qio-ui-radius);
  background: transparent;
  transition: background var(--dur-fast) var(--ease-1);
}
.splitter:hover::after,
.splitter:focus-visible::after {
  background: var(--border-strong);
}
.splitter:active::after {
  background: var(--accent);
}
.splitter:focus-visible { outline: none; }
[data-dragging="left"] .center-pane,
[data-dragging="right"] .center-pane { user-select: none; }
[data-dragging] .splitter { cursor: col-resize; }

/* ---- 收起后的任务入口 ---- */
.collapsed-rail {
  flex: none;
  width: 48px;
  /* 收起后仍保留和其他卡片一样的 12px 槽位，避免左→中变成 0 */
  margin-right: var(--conversation-card-gap, 12px);
  display: flex;
  flex-direction: column;
  align-items: center;
  /* FU5：任务按钮就是收起栏本身 —— 不再在栏里再套一个小圆角按钮。
     栏与按钮用同一圈圆角、同一条边：按钮铺满整栏，圆角完全对齐。 */
  padding: 0;
  overflow: hidden;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-surface);
}
.rail-btn {
  position: relative;
  width: 100%;
  flex: 1;
  padding: 14px 0;
  border: 0;
  border-radius: inherit;
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  cursor: pointer;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center; /* 图标 + 「任务」整组在收起栏里垂直居中 */
  gap: 5px;
}
.rail-btn svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.rail-btn:hover { color: var(--text-strong); background: var(--layer-hover); }
.rail-btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: -2px; }
/* 竖排「任务」：竖排行盒给一个偶数宽度（line-height:16px）——在 46px 宽的按钮里
   从整数像素开始、左右对称留白，避免半像素盒宽在高 DPI 下看起来偏左/右；
   再对字体墨迹的轻微右侧偏重做 0.5px 光学补偿。 */
.rail-label {
  writing-mode: vertical-rl;
  letter-spacing: 0.14em;
  line-height: 16px;
  text-align: center;
  transform: translateX(-0.5px);
}
.rail-badge {
  position: absolute;
  /* 收起栏 overflow:hidden（保证按钮与栏圆角完全对齐），徽标改为贴内侧右上角 */
  top: 4px;
  right: 4px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: var(--r-pill);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  color: var(--text-secondary);
  font-size: 9.5px;
  line-height: 14px;
  text-align: center;
}
.rail-badge[data-tone="attention"] {
  color: var(--warning);
  border-color: var(--warning);
  background: var(--warning-soft);
}
.rail-badge[data-tone="active"] {
  color: var(--accent);
  border-color: var(--accent);
  background: var(--accent-soft);
}

/* ---- 全窗口预览 ---- */
.preview-overlay {
  position: absolute;
  /* 与话题/左栏同一条卡片顶线：y=82；左右/底边与全局栏同一 8px 外沿 */
  inset: 2px 8px 8px;
  z-index: 30;
  border-radius: var(--qio-ui-radius);
  overflow: hidden;
  box-shadow: var(--shadow-3);
}

/* ---- 任务抽屉（窄窗口临时展开，不写偏好） ---- */
.rail-drawer-layer {
  position: absolute;
  inset: 0;
  z-index: 35;
  display: flex;
}
.rail-drawer-backdrop {
  position: absolute;
  inset: 0;
  background: var(--bg-overlay);
}
.rail-drawer {
  position: absolute;
  top: 6px;
  bottom: 0;
  left: 8px;
  max-width: calc(100vw - 24px);
  display: flex;
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-surface);
  box-shadow: var(--shadow-3);
  overflow: hidden;
  animation: drawer-in var(--dur-panel) var(--ease-1) both;
}
@keyframes drawer-in {
  from { transform: translateX(calc(-1 * var(--shift-panel))); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}

/* ---- 预览栏收起的贴边入口（中线偏上，避让右下角星球入口） ---- */
.preview-reopen {
  position: absolute;
  right: 0;
  top: 22%;
  z-index: 8;
  display: inline-flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  width: 26px;
  padding: 10px 0;
  border: 1px solid var(--border-subtle);
  border-right: 0;
  border-radius: var(--qio-ui-radius) 0 0 var(--r-sm);
  background: var(--bg-elevated);
  color: var(--text-muted);
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.preview-reopen svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.preview-reopen-count { font-size: 9.5px; }
.preview-reopen:hover {
  color: var(--text-strong);
  border-color: var(--border-strong);
}
.preview-reopen:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: -2px; }
.preview-reopen .badge { font-size: 10px; color: var(--text-muted); }

/* ---- 未完成事项（恢复区） ---- */
.recovery-layer {
  position: absolute;
  inset: 0;
  z-index: 40;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.recovery-backdrop {
  position: absolute;
  inset: 0;
  background: var(--bg-overlay);
  backdrop-filter: blur(2px);
}
.recovery-panel {
  position: relative;
  width: min(680px, 100%);
  max-height: min(80vh, 720px);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-4) var(--sp-5);
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-3);
  overflow-y: auto;
}
.recovery-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.recovery-head h2 {
  margin: 0;
  font-family: var(--serif);
  font-size: var(--fs-lg);
  font-weight: 500;
  color: var(--text-strong);
}
</style>
