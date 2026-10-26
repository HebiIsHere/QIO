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
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import router from "../router";
import brandLogo from "../assets/qio-orbit-logo.png";
import PermissionModeText from "./security/PermissionModeText.vue";
import { useUiStore } from "../stores/ui";
import { useEventStore } from "../stores/events";
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
const notice = ref("");

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
/** 标题条 X / 系统关闭的统一动作：把关闭请求交给壳，由壳按设置执行。 */
async function requestWindowClose() {
  if (!shell || busy.value) return;
  try {
    await shell.close();
  } catch (err) {
    // 窗口接口失败（极少见）：保留可查原因，不假装已关闭。
    console.error("[qio] 发送窗口关闭请求失败", err);
    notice.value = "无法关闭窗口（详见日志）";
  }
}

/** 壳发来 ask 问询：先 ACK（壳 1.8s 没收到会兜底隐藏到托盘），再弹确认框。 */
async function onShellCloseRequested() {
  if (busy.value || askOpen.value) return;
  void ackCloseRequest();
  askOpen.value = true;
}

/** ask 里选「最小化到托盘」：由壳执行 hide（前端只发动作，不判断设置）。 */
async function chooseTray() {
  if (busy.value) return;
  askOpen.value = false;
  notice.value = "";
  try {
    await requestClose("tray");
  } catch (err) {
    console.error("[qio] 隐藏到托盘失败", err);
    notice.value = "无法隐藏到系统托盘（详见日志）";
  }
}

/** ask 里选「退出 QIO」：壳自己完成后端收尾/进程树核验/退出；前端只置守卫。 */
async function chooseQuit() {
  if (busy.value) return;
  askOpen.value = false;
  busy.value = true;
  notice.value = "";
  try {
    await requestClose("quit");
    // 壳会 app.exit(0)：正常路径下这里不会返回；返回说明退出没完成，保留提示。
  } catch (err) {
    console.error("[qio] 请求退出失败（壳可能已经退出）", err);
  }
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
  askOpen.value = false;
  rememberChoice.value = false;
  notice.value = "";
}

function behaviorLabel(value: CloseBehavior): string {
  return CLOSE_BEHAVIOR_OPTIONS.find((o) => o.value === value)?.label ?? value;
}

function onKeydown(e: KeyboardEvent) {
  if (askOpen.value && e.key === "Escape") {
    e.preventDefault();
    cancelAsk();
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
  unlisten?.();
  unlisten = null;
  unlistenClose?.();
  unlistenClose = null;
  unlistenTrayQuit?.();
  unlistenTrayQuit = null;
  window.removeEventListener("keydown", onKeydown, true);
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
      <button class="wc-btn" type="button" aria-label="最小化" title="最小化" @click="run('minimize')">
        <svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2.5 6h7" /></svg>
      </button>
      <button
        class="wc-btn"
        type="button"
        :aria-label="maximized ? '向下还原' : '最大化'"
        :title="maximized ? '向下还原' : '最大化'"
        @click="run('toggleMaximize')"
      >
        <svg v-if="!maximized" viewBox="0 0 12 12" aria-hidden="true"><rect x="2.5" y="2.5" width="7" height="7" rx="1" /></svg>
        <svg v-else viewBox="0 0 12 12" aria-hidden="true">
          <rect x="2.5" y="4.5" width="5" height="5" rx="1" />
          <path d="M4.5 4.5V3.6A1.1 1.1 0 0 1 5.6 2.5h2.8A1.1 1.1 0 0 1 9.5 3.6v2.8A1.1 1.1 0 0 1 8.4 7.5h-.9" />
        </svg>
      </button>
      <!-- R9 §11：发窗口关闭请求，由壳按 ui.close_behavior 执行（ask 才回来弹框） -->
      <button class="wc-btn wc-close" type="button" aria-label="关闭" title="关闭窗口" @click="requestWindowClose">
        <svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 3l6 6M9 3l-6 6" /></svg>
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
      <!-- 权限模式：纯文字，常驻全局栏（对话页 / 互动页同一处）；不再用页面内胶囊徽标。 -->
      <PermissionModeText class="chrome-mode" />
      <button
        class="chrome-btn"
        type="button"
        aria-label="设置"
        title="打开设置"
        @click="openSettings"
      >
        <!-- 设置：居中齿轮（不是太阳/亮度图标）；24 视框保证齿与内圆严格同心 -->
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path
            d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"
          />
          <circle cx="12" cy="12" r="3" />
        </svg>
      </button>
      <span class="chrome-sep" aria-hidden="true"></span>
    </div>

    <!-- R9 §11：关闭确认框（关闭行为 = ask）。三个选择与契约一字不差：最小化到托盘 / 退出 QIO / 取消 -->

    <!-- 关闭确认框（关闭行为 = ask）。三个选择与契约一字不差：最小化到托盘 / 退出 QIO / 取消 -->
    <Transition name="qio-fade">
      <div v-if="askOpen" class="close-ask-scrim">
        <div
          class="close-ask qio-floating"
          role="dialog"
          aria-modal="true"
          aria-labelledby="qio-close-ask-title"
          aria-describedby="qio-close-ask-desc"
        >
          <p id="qio-close-ask-title" class="close-ask__title">关闭 QIO 主窗口？</p>
          <p id="qio-close-ask-desc" class="close-ask__desc">
            可以只把窗口收进系统托盘继续运行，也可以直接退出。
            「退出 QIO」会先保存状态、有界收尾，再结束本实例的后端进程。
          </p>
          <div class="close-ask__actions">
            <button class="qio-btn primary" type="button" autofocus @click="chooseClose('tray')">
              最小化到托盘
            </button>
            <button class="qio-btn danger" type="button" @click="chooseClose('quit')">
              退出 QIO
            </button>
            <button class="qio-btn quiet" type="button" @click="cancelAsk">取消</button>
          </div>
          <label class="close-ask__remember">
            <input v-model="rememberChoice" type="checkbox" />
            记住我的选择（以后按「{{ behaviorLabel(ui.closeBehavior) }}」处理）
          </label>
          <p v-if="notice" class="close-ask__notice" role="status">{{ notice }}</p>
        </div>
      </div>
    </Transition>

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
.chrome-btn {
  pointer-events: auto;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: var(--r-sm);
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
  border-radius: var(--r-sm);
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

/* 关闭确认框：盖住整个窗口（fixed 相对视口），比标题条自身再高一层。 */
.close-ask-scrim {
  position: fixed;
  inset: 0;
  z-index: 2;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--bg-overlay);
}
.close-ask {
  width: min(420px, 100%);
  padding: 20px 22px 18px;
  border-radius: var(--r-lg);
  border: 1px solid var(--border-strong);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-float);
  color: var(--text-secondary);
}
.close-ask__title {
  margin: 0 0 8px;
  font-family: var(--serif);
  font-size: 16px;
  color: var(--text-strong);
}
.close-ask__desc {
  margin: 0 0 16px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-secondary);
}
.close-ask__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.close-ask__remember {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-top: 14px;
  font-size: 12px;
  color: var(--text-muted);
  cursor: pointer;
}
.close-ask__remember input {
  accent-color: var(--accent);
}
.close-ask__notice {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--danger);
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
  border-radius: var(--r-md);
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
