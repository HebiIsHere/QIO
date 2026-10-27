<script setup lang="ts">
/**
 * 顶部全局栏（窗口层）—— 无边框窗口（tauri.conf.json: `decorations: false`）的页眉。
 *
 * 对话页与互动页**共用同一条全局栏**（互动页 v2 契约 §2.1）：同一张圆角栏、同一份品牌
 * （轨道标志 + PERSONAL AGENT）、同一个设置入口与同一组窗口控制。互动页因此不再需要
 * 自己再画一条窗口标题栏。
 *
 * 这里承担五件事：
 * 1. 可拖拽面（窗口没有系统标题栏，拖窗口只能靠它）；
 * 2. 最小化 / 最大化还原 / 关闭三个窗口按钮（Tauri 语义保持原样，R9 会替换关闭/托盘实现）；
 * 3. 品牌（轨道标志 + PERSONAL AGENT，不放副标题，也不显示当前话题标题 —— 话题在页面里）；
 * 4. 设置入口（对话 / 互动页显示；打开现有完整设置页，返回时各 store 保留草稿/附件/阅读位置）；
 * 5. 互动页的 `#qio-interactive-chrome-extra` 挂载点（搜索 / 保存状态由互动页 Teleport 进来，
 *    位于品牌与设置之间；对话页不渲染它，现有视觉不回退）。
 *
 * 拖拽面与按钮是**兄弟节点**而不是父子：Tauri 的拖拽区按命中元素判定，
 * 按钮/挂载点若在拖拽面内部，点击就会先被拖拽逻辑截走（不同版本判定 `hasAttribute`
 * 或 `closest` 的行为还不一致），拆开两种判定都安全。字标同理：pointer-events 关掉，
 * 点在字标上也还能拖窗口。
 *
 * 高度：对话页与互动页都需要一条真正放得下字标、设置入口与顶栏控件的全局栏，所以在这两个
 * 路由把 `--titlebar-height` 调高（documentElement 内联变量），离开时恢复。其他页面的
 * 标题条高度、定位与交互完全不变。
 *
 * R9 §11（W5）：关闭按钮**不自己判断行为**，而是发窗口关闭请求（`window.close()`）——
 * 由壳在 CloseRequested 里按 ui.close_behavior 执行：tray 直接 hide、quit 直接退出，
 * ask 才发 `qio://close-requested` 回来让前端弹确认框。这一层只承担 ask 的 UI，
 * 外观（品牌 / 设置入口 / 全局栏高度）用页面线 v2 的版本。
 *
 * 真机 install_e2e 的教训：关闭行为不能依赖前端是否还能正常 invoke / 读设置 ——
 * 前端启动握手异常时点 X 必须仍有反应（壳侧独立执行）。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import router from "../router";
import brandLogo from "../assets/qio-orbit-logo.png";
import PermissionModeText from "./security/PermissionModeText.vue";
import { useUiStore } from "../stores/ui";
import { useEventStore } from "../stores/events";
import { useUpdaterStore } from "../stores/updater";
import { useModalArbiterStore } from "../stores/modalArbiter";
import { useNotificationsStore } from "../stores/notifications";
import QIcon from "./ui/QIcon.vue";
import GlobalIssueEntry from "./feedback/GlobalIssueEntry.vue";
import { vQioTip } from "../composables/useQioTip";
import { CLOSE_BEHAVIOR_OPTIONS, type CloseBehavior } from "../services/quitGuard";
import {
  ackCloseRequest,
  listenShellEvent,
  noteShellQuit,
  requestClose,
} from "../services/desktop";

/** @tauri-apps/api/window 里我们真正用到的那几个方法（只声明用到的部分，便于测试替身）。 */
interface ShellWindow {
  minimize(): Promise<void>;
  toggleMaximize(): Promise<void>;
  close(): Promise<void>;
  isMaximized(): Promise<boolean>;
  onResized(handler: () => void): Promise<() => void>;
}

/** 全局栏（对话页与互动页共用）的标题条高度：8px 外边距 + 52px 圆角栏 + 10px 呼吸，给下方卡片留出同一条顶线。 */
const GLOBAL_BAR_TITLEBAR_HEIGHT = "70px";

function tauriInternals(): unknown {
  return (globalThis as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
}

/** 是否在 Tauri 壳里。浏览器开发预览（IAB）里没有它，此时不画窗口按钮：
 *  画了也点不动 —— 那正是「看起来能用、其实不能用」。 */
const inShell = ref(false);
const maximized = ref(false);
const ui = useUiStore();

/** 关闭确认框（仅在关闭行为 = ask 时出现）。 */
const askOpen = ref(false);
const rememberChoice = ref(false);
const busy = ref(false);
/**
 * 标题条操作的真实失败（可见、可重试、可取消）。
 *
 * 以前这些提示写在 ask 弹窗里：弹窗一关就再也看不到（失败被藏进一个已经不可见的 DOM），
 * 退出失败时 busy 还会一直是 true。现在统一放在标题条下方的浮动状态条里：
 * 失败原因 + 真实的重新动作 + 取消（只关闭这条提示，不假装已经成功）。
 */
type ChromeFailureKind = "close" | "tray" | "quit";
interface ChromeFailure {
  kind: ChromeFailureKind;
  text: string;
  /** 重试文案（动作真实可重放：同一个壳请求，不是假按钮） */
  retryLabel: string;
}
const chromeFailure = ref<ChromeFailure | null>(null);
/** 退出请求 10 秒仍未结束：应用还在运行，就必须结束「正在退出」的等待态并给可重试入口。 */
const QUIT_PENDING_TIMEOUT_MS = 10_000;
let quitTimer: ReturnType<typeof setTimeout> | null = null;

const updater = useUpdaterStore();
const arbiter = useModalArbiterStore();
/** 关闭询问在仲裁器里的稳定身份（最高优先级 close-ask）。 */
const CLOSE_ASK_ID = "qio-close-ask";
/** 后台发现更新：只在设置入口留一个小标记（不弹通知、不自动打开更新内容）。 */
const updateAvailable = computed(() => updater.hasUpdate);
const settingsLabel = computed(() =>
  updateAvailable.value ? "设置（有可用更新）" : "设置",
);

/** 是否共用这条全局栏：对话页与互动页（其他页的页眉保持原样）。 */
const hasGlobalBar = computed(() => {
  const name = router.currentRoute.value.name;
  return name === "conversation" || name === "interactive";
});

/** 互动页专属：搜索 / 保存状态的顶栏挂载点，对话页不渲染（现有视觉不回退）。 */
const isInteractive = computed(() => router.currentRoute.value.name === "interactive");

let shell: ShellWindow | null = null;
let unlisten: (() => void) | null = null;
let unlistenClose: (() => void) | null = null;
let unlistenTrayQuit: (() => void) | null = null;
let disposed = false;
/** 进入对话页前 documentElement 上的内联标题条高度（离开时原样恢复）。 */
let previousTitlebarHeight: string | null = null;
let previousTitlebarPriority = "";

async function syncMaximized() {
  if (!shell) return;
  try {
    maximized.value = await shell.isMaximized();
  } catch {
    // 读不到就保持上一次的样子：图标不准比按钮失灵轻得多
  }
}

async function run(action: "minimize" | "toggleMaximize") {
  if (!shell) return;
  try {
    await shell[action]();
    await syncMaximized();
  } catch (err) {
    // 不静默：窗口按钮失灵时至少要留下可查的原因（多半是 capabilities 少了权限）
    console.error("[qio] 窗口操作 " + action + " 失败", err);
  }
}

function openSettings() {
  if (!hasGlobalBar.value) return;
  void router.push({ name: "settings" });
}

/**
 * 只在共用全局栏的路由（对话 / 互动）加高标题条。
 *
 * 直接改 documentElement 的内联变量：App.vue 的 padding-top 与自家 height 读同一个
 * 令牌，两边自动一致；两个路由之间切换不闪动（高度相同），离开到设置/星球等页面时
 * 恢复原值。
 */
function applyGlobalBarChrome(on: boolean) {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  if (on) {
    if (previousTitlebarHeight === null) {
      previousTitlebarHeight = root.style.getPropertyValue("--titlebar-height");
      previousTitlebarPriority = root.style.getPropertyPriority("--titlebar-height");
    }
    root.style.setProperty("--titlebar-height", GLOBAL_BAR_TITLEBAR_HEIGHT);
    return;
  }
  if (previousTitlebarHeight === null) return;
  if (previousTitlebarHeight) {
    root.style.setProperty("--titlebar-height", previousTitlebarHeight, previousTitlebarPriority);
  } else {
    root.style.removeProperty("--titlebar-height");
  }
  previousTitlebarHeight = null;
  previousTitlebarPriority = "";
}

watch(hasGlobalBar, (value) => applyGlobalBarChrome(value), { immediate: true });

// ── R9 §11：关闭请求 / ask 确认框（壳负责真正的关闭行为，这里只做 UI）─────────────

function clearQuitTimer() {
  if (quitTimer) {
    clearTimeout(quitTimer);
    quitTimer = null;
  }
}

/** 退出请求发出后应用仍在运行：结束等待态，显示可重试/可取消的真实状态。 */
function noteQuitStillRunning() {
  clearQuitTimer();
  busy.value = false;
  chromeFailure.value = {
    kind: "quit",
    text: "退出还没有完成：QIO 仍在运行。可以重试，或先取消继续使用。",
    retryLabel: "重试退出",
  };
}

/** 标题条 X / 系统关闭的统一动作：把关闭请求交给壳，由壳按设置执行。 */
async function requestWindowClose() {
  if (!shell || busy.value) return;
  chromeFailure.value = null;
  try {
    await shell.close();
  } catch (err) {
    // 窗口接口失败（极少见）：保留可查原因，不假装已关闭。
    console.error("[qio] 发送窗口关闭请求失败", err);
    chromeFailure.value = {
      kind: "close",
      text: "无法关闭窗口（详见日志）。可以重试；如果持续失败，可用系统托盘或任务管理器退出。",
      retryLabel: "重试关闭",
    };
  }
}

/** 壳发来 ask 问询：先 ACK（壳 1.8s 没收到会兜底隐藏到托盘），再弹确认框。 */
async function onShellCloseRequested() {
  if (busy.value || askOpen.value) return;
  chromeFailure.value = null;
  void ackCloseRequest();
  enterCloseAsk();
}

/** 最小化到托盘：由壳执行 hide（前端只发动作，不判断设置）。失败必须仍可见并可重试。 */
async function chooseTray() {
  if (busy.value) return;
  if (askOpen.value) leaveCloseAsk();
  chromeFailure.value = null;
  try {
    await requestClose("tray");
  } catch (err) {
    console.error("[qio] 隐藏到托盘失败", err);
    chromeFailure.value = {
      kind: "tray",
      text: "无法隐藏到系统托盘（详见日志）。可以重试，或直接用窗口按钮。",
      retryLabel: "重试隐藏到托盘",
    };
  }
}

/** 退出 QIO：壳自己完成后端收尾/进程树核验/退出；前端只置守卫。 */
async function chooseQuit() {
  if (busy.value) return;
  if (askOpen.value) leaveCloseAsk();
  chromeFailure.value = null;
  busy.value = true;
  // 有界等待：壳正常会在预算内退出（页面不再返回）；如果 10 秒后页面还在，
  // 说明退出没有完成 —— 必须结束「正在退出」并给出重试，而不是永远锁死 busy。
  clearQuitTimer();
  quitTimer = setTimeout(noteQuitStillRunning, QUIT_PENDING_TIMEOUT_MS);
  try {
    await requestClose("quit");
    // 壳会 app.exit(0)：正常路径下这里不会返回；返回说明退出没完成。
    noteQuitStillRunning();
  } catch (err) {
    console.error("[qio] 请求退出失败（壳可能已经退出）", err);
    clearQuitTimer();
    busy.value = false;
    chromeFailure.value = {
      kind: "quit",
      text: "退出请求没有完成：QIO 仍在运行。可以重试，或先取消继续使用。",
      retryLabel: "重试退出",
    };
  }
}

/** 失败状态条上的「重试」：重放同一个真实动作（不是假按钮）。 */
async function retryChromeAction() {
  const failure = chromeFailure.value;
  if (!failure || busy.value) return;
  chromeFailure.value = null;
  if (failure.kind === "close") {
    await requestWindowClose();
    return;
  }
  if (failure.kind === "tray") {
    await chooseTray();
    return;
  }
  await chooseQuit();
}

function dismissChromeFailure() {
  chromeFailure.value = null;
}

async function chooseClose(action: "tray" | "quit") {
  if (busy.value) return;
  if (rememberChoice.value) {
    try {
      // setCloseBehavior 会同时把新值同步给壳（下次点 X 立即按新行为）。
      await ui.setCloseBehavior(action);
    } catch (err) {
      // 记忆失败不影响本次选择，但要留下可查原因（下次还会问）。
      console.error("[qio] 保存关闭行为失败", err);
    }
  }
  rememberChoice.value = false;
  if (action === "tray") await chooseTray();
  else await chooseQuit();
}

function cancelAsk() {
  if (busy.value) return;
  rememberChoice.value = false;
  chromeFailure.value = null;
  leaveCloseAsk();
}

/**
 * 关闭询问是最高层触发器（kind=close-ask，优先级 100）：
 * - 先向仲裁器注册；被 false 挡住时**不强行显示**（壳 1.8s 后会兜底隐藏到托盘）；
 * - 注册成功后 suspendAll("close-ask")：业务弹窗只隐藏（v-show），DOM/状态/草稿都不丢；
 * - 取消 / 完成（成功或失败）后 resumeAll()，并让排队的通知重新有机会显示。
 *
 * F01 说明：close-ask 自己是「整体挂起」的发起方，因此它的显示条件**不能**用
 * arbiter.isActive/isVisible 判断（suspendAll 之后栈内任何项都不是活动项，包括发起者）。
 * 它的可见条件就是「open() 成功 + askOpen」：同一时刻它是唯一最高优先级层，
 * 注册被拒时不会显示。
 */
function enterCloseAsk(): boolean {
  if (askOpen.value) return true;
  const opened = arbiter.open({
    id: CLOSE_ASK_ID,
    kind: "close-ask",
    dismissible: true,
  });
  if (!opened) {
    // 被更高/同优先级弹窗挡住：不显示（也不叠层抢焦点）。壳会自己走兜底。
    console.warn("[qio] 关闭询问被其它中央弹窗挡住，未显示");
    return false;
  }
  arbiter.suspendAll("close-ask");
  askOpen.value = true;
  return true;
}

function leaveCloseAsk() {
  askOpen.value = false;
  arbiter.close(CLOSE_ASK_ID);
  arbiter.resumeAll();
  // 弹窗结束后通知可以继续排队显示（B 的通知 host 也会在合适的时机 flush；
  // 这里补一次，保证「关闭询问期间入队」的通知不会一直等下去）。
  try {
    useNotificationsStore().flushQueue();
  } catch {
    /* 通知 store 不可用不影响关闭流程 */
  }
}

function behaviorLabel(value: CloseBehavior): string {
  return CLOSE_BEHAVIOR_OPTIONS.find((o) => o.value === value)?.label ?? value;
}

/** 关闭询问的焦点管理：打开前记住焦点、打开后聚焦「取消」、关闭后归还。 */
const askDialogRef = ref<HTMLElement | null>(null);
const askCancelRef = ref<HTMLButtonElement | null>(null);
let askRestoreFocusTo: HTMLElement | null = null;

function focusablesWithin(root: HTMLElement | null): HTMLElement[] {
  if (!root) return [];
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
    ),
  );
}

function rememberAskFocus() {
  const active = document.activeElement;
  askRestoreFocusTo = active instanceof HTMLElement ? active : null;
}

function restoreAskFocus() {
  const el = askRestoreFocusTo;
  askRestoreFocusTo = null;
  if (el && el.isConnected) {
    el.focus();
    return;
  }
  // 触发元素可能已经不存在（例如窗口控制被重渲染）：退回到设置入口或整条栏。
  const fallback =
    document.querySelector<HTMLElement>('button[aria-label^="设置"]') ?? null;
  if (fallback && fallback.isConnected) fallback.focus();
}

watch(askOpen, async (open) => {
  if (open) {
    rememberAskFocus();
    await nextTick();
    askCancelRef.value?.focus();
    return;
  }
  restoreAskFocus();
});

function onKeydown(e: KeyboardEvent) {
  if (!askOpen.value) return;
  if (e.key === "Escape") {
    e.preventDefault();
    if (!busy.value) cancelAsk();
    return;
  }
  // 模态焦点限制：Tab 只在关闭询问内循环，不跑到后面的页面/业务弹窗上。
  if (e.key !== "Tab") return;
  const root = askDialogRef.value;
  const items = focusablesWithin(root);
  if (!root || !items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  const active = document.activeElement as HTMLElement | null;
  if (e.shiftKey && (active === first || !root.contains(active))) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && active === last) {
    e.preventDefault();
    first.focus();
  }
}

onMounted(async () => {
  if (!tauriInternals()) return;
  try {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    const win = getCurrentWindow() as unknown as ShellWindow;
    if (disposed) return;
    shell = win;
    inShell.value = true;
    await syncMaximized();
    // 拖边缩放、Win+方向键、双击标题条都可能改变最大化状态：跟着系统走
    unlisten = await win.onResized(() => void syncMaximized());
    if (disposed) {
      unlisten?.();
      unlisten = null;
    }
    // 系统关闭（Alt+F4 / 任务栏关闭 / WM_CLOSE / 标题条 X）由壳按设置执行；
    // 只有 ask 档会发这个事件回来 —— 前端只负责 ACK + 渲染确认框。
    unlistenClose = await listenShellEvent("qio://close-requested", () => {
      void onShellCloseRequested();
    });
    // 壳侧退出（托盘菜单 / 系统关闭 quit）：前端只置守卫、断开 SSE 长连接，
    // 让后端 uvicorn 不必等这条连接就能在预算内退出（否则只能被超时强杀）。
    unlistenTrayQuit = await listenShellEvent("qio://quit-requested", () => {
      noteShellQuit();
      busy.value = true;
      try {
        useEventStore().disconnect();
      } catch (err) {
        console.warn("[qio] 退出时断开事件流失败（不影响壳侧退出）", err);
      }
    });
    window.addEventListener("keydown", onKeydown, true);
  } catch (err) {
    console.error("[qio] 窗口标题条初始化失败", err);
    shell = null;
    inShell.value = false;
  }
});

onBeforeUnmount(() => {
  disposed = true;
  clearQuitTimer();
  unlisten?.();
  unlisten = null;
  unlistenClose?.();
  unlistenClose = null;
  unlistenTrayQuit?.();
  unlistenTrayQuit = null;
  window.removeEventListener("keydown", onKeydown, true);
  if (askOpen.value) leaveCloseAsk();
  shell = null;
  applyGlobalBarChrome(false);
});
</script>

<template>
  <div
    class="window-chrome"
    :class="{ 'conversation-bar': hasGlobalBar, 'interactive-bar': isInteractive }"
  >
    <!-- 拖拽面：双击由 Tauri 内置逻辑转成「最大化/还原」 -->
    <div class="drag-surface" data-tauri-drag-region aria-hidden="true"></div>
    <div v-if="inShell" class="window-controls">
      <button
        class="wc-btn"
        type="button"
        aria-label="最小化"
        v-qio-tip="'把窗口最小化到任务栏（QIO 继续在后台运行）'"
        @click="run('minimize')"
      >
        <QIcon name="window-minimize" :size="12" />
      </button>
      <button
        class="wc-btn"
        type="button"
        :aria-label="maximized ? '向下还原' : '最大化'"
        v-qio-tip="maximized ? '把窗口还原到原来的大小' : '把窗口最大化（也可以双击标题条）'"
        @click="run('toggleMaximize')"
      >
        <QIcon :name="maximized ? 'window-restore' : 'window-maximize'" :size="12" />
      </button>
      <!-- R9 §11：发窗口关闭请求，由壳按 ui.close_behavior 执行（ask 才回来弹框） -->
      <button
        class="wc-btn wc-close"
        type="button"
        aria-label="关闭"
        v-qio-tip="'关闭主窗口（按设置里的关闭行为处理；可能只是收进托盘）'"
        @click="requestWindowClose"
      >
        <QIcon name="window-close" :size="12" />
      </button>
    </div>

    <!-- 对话页 / 互动页：左侧用轨道标志 + PERSONAL AGENT；其他页保持原来克制的 QIO 字标。
         pointer-events 关掉，点在品牌区上仍可拖窗口。 -->
    <div class="brand" aria-hidden="true">
      <template v-if="hasGlobalBar">
        <img class="brand-logo" :src="brandLogo" alt="" />
        <span class="brand-sep"></span>
        <span class="brand-caption">PERSONAL AGENT</span>
      </template>
      <template v-else>
        <span class="brand-dot"></span>
        <span class="brand-name">QIO</span>
      </template>
    </div>

    <!--
      互动页：搜索 / 保存状态挂载点（互动页把控件 Teleport 到这里）。
      位置在品牌与设置之间；拖动面是它的兄弟，pointer-events 在样式里显式打开，
      点搜索框/保存状态不会被窗口拖拽吃掉。对话页不渲染它，现有视觉不回退。
    -->
    <div v-if="isInteractive" id="qio-interactive-chrome-extra" class="chrome-extra"></div>

    <!-- 设置入口：对话页 / 互动页出现，固定排在窗口按钮左边（最右仍是窗口控制）。 -->
    <div v-if="hasGlobalBar" class="chrome-actions" :class="{ 'with-controls': inShell }">
      <!-- 全局异常入口（B）：同一根因去重后的独立问题数量；无问题时不渲染、不占位 -->
      <GlobalIssueEntry />
      <!-- 权限模式：纯文字，常驻全局栏（对话页 / 互动页同一处）；不再用页面内胶囊徽标。 -->
      <PermissionModeText class="chrome-mode" />
      <button
        class="chrome-btn"
        type="button"
        :aria-label="settingsLabel"
        :data-has-update="updateAvailable ? '1' : '0'"
        v-qio-tip="updateAvailable ? '打开设置（有可用更新，在「数据与维护」里查看）' : '打开设置'"
        @click="openSettings"
      >
        <QIcon name="settings" :size="16" />
        <!-- 后台发现更新：只在这个入口留一个小标记；不弹通知、不自动打开更新内容 -->
        <span v-if="updateAvailable" class="chrome-update-dot" aria-hidden="true"></span>
        <span class="sr-only" v-if="updateAvailable">有可用更新</span>
      </button>
      <span class="chrome-sep" aria-hidden="true"></span>
    </div>

    <!-- 非对话/互动页（设置/调试等）：全局异常入口仍然只能有一个；
         这里不渲染设置按钮（设置页本身就是设置），但连接/模型/更新等整体异常
         必须在任何页面都看得见，而不是只在对话页顶部栏。 -->
    <div
      v-if="!hasGlobalBar"
      class="chrome-actions chrome-actions--minimal"
      :class="{ 'with-controls': inShell }"
    >
      <GlobalIssueEntry />
    </div>

    <!-- 关闭确认框（关闭行为 = ask）：三个选择明文保留（最小化到托盘 / 退出 QIO / 取消）。
         只说明「退出会结束当前运行」，不展示任务概况/名称/数量。 -->
    <Transition name="qio-fade">
      <div v-if="askOpen" class="close-ask-scrim">
        <div
          ref="askDialogRef"
          class="close-ask qio-floating"
          role="dialog"
          aria-modal="true"
          aria-labelledby="qio-close-ask-title"
          aria-describedby="qio-close-ask-desc"
        >
          <p id="qio-close-ask-title" class="close-ask__title">关闭 QIO 主窗口？</p>
          <p id="qio-close-ask-desc" class="close-ask__desc">
            最小化到托盘：窗口收起，QIO 继续在后台运行。<br />
            退出 QIO：结束当前运行。
          </p>
          <div class="close-ask__actions">
            <button
              class="qio-btn primary close-ask__btn"
              type="button"
              :disabled="busy"
              @click="chooseClose('tray')"
            >
              最小化到托盘
            </button>
            <button
              class="qio-btn danger close-ask__btn"
              type="button"
              :disabled="busy"
              @click="chooseClose('quit')"
            >
              退出 QIO
            </button>
            <button
              ref="askCancelRef"
              class="qio-btn quiet close-ask__btn"
              type="button"
              :disabled="busy"
              @click="cancelAsk"
            >
              取消
            </button>
          </div>
          <label class="close-ask__remember">
            <input v-model="rememberChoice" class="close-ask__check" type="checkbox" :disabled="busy" />
            <span>记住我的选择（以后按「{{ behaviorLabel(ui.closeBehavior) }}」处理）</span>
          </label>
        </div>
      </div>
    </Transition>

    <!-- 标题条操作失败：留在仍可见的界面里，给真实重试与取消；不写进已关闭的弹窗、不永远 busy。 -->
    <div v-if="chromeFailure" class="chrome-failure" role="alert">
      <span class="chrome-failure__text">{{ chromeFailure.text }}</span>
      <span class="chrome-failure__actions">
        <button
          class="qio-btn mini chrome-failure__retry"
          type="button"
          :disabled="busy"
          @click="retryChromeAction"
        >
          {{ chromeFailure.retryLabel }}
        </button>
        <button
          class="qio-btn mini quiet chrome-failure__cancel"
          type="button"
          :disabled="busy"
          @click="dismissChromeFailure"
        >
          取消
        </button>
      </span>
    </div>

    <!-- 退出进行中：给用户一个明确的「正在退出」反馈；此期间按钮不可再触发 -->
    <div v-if="busy" class="close-busy" role="status">
      <span class="close-busy__dot" aria-hidden="true"></span>
      正在退出 QIO…
    </div>
  </div>
</template>

<style scoped>
/* 窗口层：高度取 --titlebar-height，透明底，永远在页面内容之上。
   层级 240 的依据（各组件现有值）：星球页 50 / 设置页与引导 60 / 等待确认入口 190 /
   模态遮罩 200。标题条属于窗口本身，比模态还高一层才符合桌面惯例 ——
   模态开着也应该能拖窗口、能关窗口。 */
.window-chrome {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  height: var(--titlebar-height);
  z-index: 240;
  user-select: none;
  -webkit-user-select: none;
}
.drag-surface {
  position: absolute;
  inset: 0;
}
/* 按钮容器只占自己那一块（贴右），**不许铺满整条**：
   flex 默认会把容器拉成整宽，那样子节点在拖拽面之上、整个条都拖不动了
   （实测：拖拽面中心的命中结果是 div.window-controls）。pointer-events 再兜一层。 */
.window-controls {
  position: absolute;
  top: 0;
  right: 0;
  height: 100%;
  z-index: 1;
  display: flex;
  align-items: stretch;
  pointer-events: none;
}
.wc-btn {
  pointer-events: auto;
  width: 46px;
  height: 100%;
  padding: 0;
  border: none;
  /* 图标按钮外轮廓统一 14px（窗口按钮虽是条状命中区，仍按同一套圆角语言） */
  border-radius: var(--qio-ui-radius);
  background: transparent;
  color: var(--text-muted);
  cursor: default;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.wc-btn svg {
  width: 12px;
  height: 12px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.1;
  stroke-linecap: round;
}
.wc-btn:hover {
  background: var(--layer-hover);
  color: var(--text-strong);
}
.wc-btn:active {
  background: var(--bg-elevated);
}
.wc-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: -2px;
}
.wc-close:hover,
.wc-close:active {
  background: var(--danger-soft);
  color: var(--danger);
}

/* ---- 品牌字标 ---- */
.brand {
  position: absolute;
  top: 0;
  left: 16px;
  height: 100%;
  display: flex;
  align-items: center;
  gap: 7px;
  pointer-events: none;
  z-index: 1;
}
.brand-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--accent);
  opacity: 0.9;
}
.brand-name {
  font-family: var(--serif);
  font-size: 14px;
  letter-spacing: 0.16em;
  line-height: 1;
  color: var(--text-strong);
  opacity: 0.86;
}

/* ---- 设置入口（对话页 / 互动页出现，其他页保持原来的短标题条） ---- */
.chrome-actions {
  position: absolute;
  top: 0;
  right: 8px;
  height: 100%;
  display: flex;
  align-items: center;
  gap: 4px;
  z-index: 1;
  pointer-events: none;
}
/* 窗口按钮占 3 × 46px：设置固定在它们左边，最右仍是窗口控制。 */
.chrome-actions.with-controls {
  right: 146px;
}
/* 非全局栏页面（设置/调试）：只放全局异常入口，位置与对话页同一套右对齐规则。 */
.chrome-actions--minimal {
  right: 8px;
}
.chrome-btn {
  position: relative;
  pointer-events: auto;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: var(--qio-ui-radius);
  background: transparent;
  color: var(--text-secondary);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease),
    border-color var(--dur-fast) var(--ease);
}
.chrome-btn svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.chrome-btn:hover {
  color: var(--text-strong);
  background: var(--layer-hover);
  border-color: var(--border-subtle);
}
.chrome-btn:active {
  background: var(--bg-elevated);
}
.chrome-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: -1px;
}
.chrome-sep {
  width: 1px;
  height: 16px;
  margin: 0 2px;
  background: var(--border-subtle);
}
/* 后台发现更新：设置入口上的一个小标记（安静、不抢焦点、不弹通知）。 */
.chrome-update-dot {
  position: absolute;
  top: 4px;
  right: 4px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 2px var(--bg-surface);
  pointer-events: none;
}
/* 只给读屏软件的文本（小标记本身 aria-hidden，语义由这里补全）。 */
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

/* ---- 对话页 / 互动页：整条全局栏是一张圆角卡片（与下方工作区卡片同一条视觉基线） ---- */
/*
 * 其他页面保持原来的 32px 透明标题条；只有共用全局栏的两个路由（对话 / 互动）把窗口层
 * 变成 52px 高的可见全局栏。这样设置入口、窗口控制在 Tauri 里仍然可点，
 * 浏览器里只留真实可用的设置入口（不画点了没反应的窗口按钮）。
 */
.window-chrome.conversation-bar {
  top: 8px;
  left: 8px;
  right: 8px;
  height: 52px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xl);
  background: var(--bg-surface);
  box-shadow: var(--shadow-1);
  overflow: hidden;
}
.conversation-bar .drag-surface {
  border-radius: inherit;
}
.conversation-bar .brand {
  left: 18px;
  gap: 9px;
}
.conversation-bar .brand-logo {
  width: auto;
  height: 30px;
  display: block;
  flex: none;
  object-fit: contain;
}
.conversation-bar .brand-dot {
  width: 8px;
  height: 8px;
  opacity: 1;
}
.conversation-bar .brand-name {
  font-size: 20px;
  font-weight: 700;
  letter-spacing: -0.02em;
  opacity: 1;
}
.brand-sep {
  width: 1px;
  height: 20px;
  margin: 0 2px;
  background: var(--border-subtle);
}
.brand-caption {
  font-family: var(--sans);
  font-size: 10px;
  line-height: 1;
  letter-spacing: 0.18em;
  color: var(--text-faint);
}
.conversation-bar .window-controls {
  right: 8px;
  align-items: center;
}
.conversation-bar .wc-btn {
  width: 44px;
  height: 38px;
  border-radius: var(--qio-ui-radius);
}
.conversation-bar .chrome-actions {
  right: 20px;
}
.conversation-bar .chrome-actions.with-controls {
  right: 152px;
}
.conversation-bar .chrome-btn {
  width: 30px;
  height: 30px;
  border-radius: 50%;
}
.conversation-bar .chrome-btn svg {
  width: 16px;
  height: 16px;
}
.conversation-bar .chrome-sep {
  height: 18px;
}

/* ---- 互动页顶栏挂载点（搜索 / 保存状态，由互动页 Teleport 注入） ---- */
/*
 * 位置：品牌与设置之间 —— 左右都靠 0 + 内容宽 + 自动外边距居中。**不用 transform**：
 * transform 会让挂载点变成 fixed 子元素的包含块，B 若用 position: fixed 画搜索结果，
 * 位置与裁切都会跟着错。
 * 命中：它是拖动面的兄弟、z-index 压过拖动面，并显式 pointer-events: auto ——
 * 点搜索框 / 保存状态不会被窗口拖拽截走。宽度只取内容（fit-content），空容器与
 * 两侧留白仍然归拖动面，整条栏照样能拖。
 * 溢出：容器本身 overflow: visible，整条栏在互动路由也覆盖成 visible（见下），
 * 否则搜索结果 / 保存说明的浮层会被圆角栏裁掉。
 * 选择：全局栏整体关掉了文本选择，但挂载点里会放真实输入框，这里恢复可选中。
 */
.chrome-extra {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  width: fit-content;
  max-width: min(560px, 50vw);
  height: 100%;
  margin: 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  z-index: 1;
  pointer-events: auto;
  user-select: text;
  -webkit-user-select: text;
  overflow: visible;
}

/* 全局栏里的权限模式文字：与设置按钮同一组，右对齐；纯文字，不加胶囊。 */
.chrome-mode {
  margin-right: 10px;
  align-self: center;
}

/* 互动路由：整条栏不裁剪挂载点里的浮层（对话页保持原来的圆角裁剪，视觉不回退）。 */
.conversation-bar.interactive-bar {
  overflow: visible;
}

/* 窄窗：品牌只留标志，副标与分隔线让位给搜索/保存；否则 480px 会互相压住。 */
@media (max-width: 760px) {
  .conversation-bar .brand {
    left: 12px;
    gap: 6px;
  }
  .conversation-bar .brand-logo {
    height: 24px;
  }
  .conversation-bar .brand-caption,
  .conversation-bar .brand-sep {
    display: none;
  }
  .conversation-bar .chrome-extra {
    max-width: min(160px, calc(100vw - 160px));
  }
  .conversation-bar .chrome-actions {
    right: 12px;
  }
}

/* ---- R9 §11：关闭确认框与退出反馈 ---- */

/* 关闭确认框：盖住整个窗口（fixed 相对视口），轻遮罩 + 轻模糊；比标题条自身再高一层。 */
.close-ask-scrim {
  position: fixed;
  inset: 0;
  z-index: 2;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--bg-overlay);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
}
/* 居中对称：标题 / 说明 / 按钮组都以同一条中轴对齐；三选项同高、等宽。 */
.close-ask {
  width: min(440px, 100%);
  padding: 22px 24px 18px;
  border-radius: var(--qio-ui-radius);
  border: 1px solid var(--border-strong);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-float);
  color: var(--text-secondary);
  text-align: center;
}
.close-ask__title {
  margin: 0 0 8px;
  font-family: var(--serif);
  font-size: 17px;
  color: var(--text-strong);
  text-align: center;
}
.close-ask__desc {
  margin: 0 0 18px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-secondary);
  text-align: center;
}
.close-ask__actions {
  display: flex;
  align-items: stretch;
  justify-content: center;
  flex-wrap: wrap;
  gap: 8px;
}
/* 三选项等宽同高：不出现「退出」比「取消」大一圈的暗示。 */
.close-ask__btn {
  flex: 1 1 0;
  min-width: 112px;
  max-width: 150px;
  height: 36px;
  white-space: nowrap;
}
/* 窄窗纵排：最小宽度下文字优先，不挤压成两行。 */
@media (max-width: 520px) {
  .close-ask__actions {
    flex-direction: column;
    align-items: stretch;
  }
  .close-ask__btn {
    flex: 0 0 auto;
    max-width: none;
    width: 100%;
  }
}
.close-ask__remember {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  margin-top: 14px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-muted);
  cursor: pointer;
  text-align: left;
}
/* 自定义外观复选：不用系统默认方框；保留真实 input 的键盘/读屏语义。 */
.close-ask__check {
  appearance: none;
  -webkit-appearance: none;
  width: 16px;
  height: 16px;
  flex: none;
  display: inline-grid;
  place-content: center;
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
  cursor: pointer;
  transition: background var(--mo-1-state) var(--ease-1), border-color var(--mo-1-state) var(--ease-1);
}
.close-ask__check::after {
  content: "";
  width: 8px;
  height: 5px;
  margin-top: -2px;
  border-left: 1.6px solid var(--on-accent);
  border-bottom: 1.6px solid var(--on-accent);
  transform: rotate(-45deg) scale(0);
  transition: transform var(--mo-1-fast) var(--ease-1);
}
.close-ask__check:checked {
  background: var(--accent);
  border-color: var(--accent);
}
.close-ask__check:checked::after {
  transform: rotate(-45deg) scale(1);
}
.close-ask__check:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.close-ask__check:disabled {
  opacity: 0.5;
  cursor: default;
}

/* ---- 标题条操作的可见失败：原因 + 真实重试 + 取消 ---- */
.chrome-failure {
  position: fixed;
  top: calc(var(--titlebar-height) + 10px);
  left: 50%;
  transform: translateX(-50%);
  z-index: 2;
  display: flex;
  align-items: center;
  gap: 10px;
  max-width: min(600px, calc(100vw - 32px));
  padding: 8px 12px;
  border: 1px solid var(--border-danger);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-float);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}
.chrome-failure__text {
  min-width: 0;
  overflow-wrap: anywhere;
}
.chrome-failure__actions {
  display: flex;
  gap: 6px;
  flex: none;
}
@media (max-width: 520px) {
  .chrome-failure {
    flex-direction: column;
    align-items: stretch;
  }
  .chrome-failure__actions {
    justify-content: flex-end;
  }
}

/* 退出进行中的轻量反馈：不拦截输入（壳马上就要退出），只说明正在发生什么。 */
.close-busy {
  position: fixed;
  left: 50%;
  bottom: 24px;
  transform: translateX(-50%);
  z-index: 2;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  border-radius: var(--qio-ui-radius);
  border: 1px solid var(--border-subtle);
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font-size: 12px;
  box-shadow: var(--shadow-float);
}
.close-busy__dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent);
  animation: close-busy-pulse 1s var(--ease) infinite alternate;
}
@keyframes close-busy-pulse {
  from {
    opacity: 0.35;
  }
  to {
    opacity: 1;
  }
}
</style>
