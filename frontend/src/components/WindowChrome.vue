<script setup lang="ts">
/**
 * 顶部全局栏（窗口层）—— 无边框窗口（tauri.conf.json: `decorations: false`）的页眉。
 *
 * 这里承担四件事：
 * 1. 可拖拽面（窗口没有系统标题栏，拖窗口只能靠它）；
 * 2. 最小化 / 最大化还原 / 关闭三个窗口按钮（Tauri 语义保持原样，R9 会替换关闭/托盘实现）；
 * 3. QIO 字标（克制的一行衬线字，不放副标题，也不显示当前话题标题 —— 话题在中央工作区顶部）；
 * 4. 设置入口（只在对话页显示；打开现有完整设置页，返回时各 store 保留草稿/附件/阅读位置）。
 *
 * 拖拽面与按钮是**兄弟节点**而不是父子：Tauri 的拖拽区按命中元素判定，
 * 按钮若在拖拽面内部，点按钮就会先被拖拽逻辑截走（不同版本判定 `hasAttribute`
 * 或 `closest` 的行为还不一致），拆开两种判定都安全。字标同理：pointer-events 关掉，
 * 点在字标上也还能拖窗口。
 *
 * 高度：对话页需要一条真正放得下字标与设置入口的全局栏，所以只在对话路由把
 * `--titlebar-height` 调高（documentElement 内联变量），离开时恢复。其他页面的
 * 标题条高度、定位与交互完全不变。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import router from "../router";
import brandLogo from "../assets/qio-orbit-logo.png";

/** @tauri-apps/api/window 里我们真正用到的那几个方法（只声明用到的部分，便于测试替身）。 */
interface ShellWindow {
  minimize(): Promise<void>;
  toggleMaximize(): Promise<void>;
  close(): Promise<void>;
  isMaximized(): Promise<boolean>;
  onResized(handler: () => void): Promise<() => void>;
}

/** 对话页的全局栏高度：8px 外边距 + 52px 圆角栏 + 10px 呼吸，给下方卡片留出同一条顶线。 */
const CONVERSATION_TITLEBAR_HEIGHT = "70px";

function tauriInternals(): unknown {
  return (globalThis as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
}

/** 是否在 Tauri 壳里。浏览器开发预览（IAB）里没有它，此时不画窗口按钮：
 *  画了也点不动 —— 那正是「看起来能用、其实不能用」。 */
const inShell = ref(false);
const maximized = ref(false);

/** 当前是否对话页（设置入口只属于对话页；其他页的页眉保持原样）。 */
const isConversation = computed(() => router.currentRoute.value.name === "conversation");

let shell: ShellWindow | null = null;
let unlisten: (() => void) | null = null;
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

async function run(action: "minimize" | "toggleMaximize" | "close") {
  if (!shell) return;
  try {
    await shell[action]();
    if (action !== "close") await syncMaximized();
  } catch (err) {
    // 不静默：窗口按钮失灵时至少要留下可查的原因（多半是 capabilities 少了权限）
    console.error("[qio] 窗口操作 " + action + " 失败", err);
  }
}

function openSettings() {
  if (!isConversation.value) return;
  void router.push({ name: "settings" });
}

/**
 * 只在对话路由加高标题条。
 *
 * 直接改 documentElement 的内联变量：App.vue 的 padding-top 与自家 height 读同一个
 * 令牌，两边自动一致；离开对话页时恢复原值，设置/星球/互动页不受影响。
 */
function applyConversationChrome(on: boolean) {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  if (on) {
    if (previousTitlebarHeight === null) {
      previousTitlebarHeight = root.style.getPropertyValue("--titlebar-height");
      previousTitlebarPriority = root.style.getPropertyPriority("--titlebar-height");
    }
    root.style.setProperty("--titlebar-height", CONVERSATION_TITLEBAR_HEIGHT);
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

watch(isConversation, (value) => applyConversationChrome(value), { immediate: true });

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
  shell = null;
  applyConversationChrome(false);
});
</script>

<template>
  <div class="window-chrome" :class="{ 'conversation-bar': isConversation }">
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
      <button class="wc-btn wc-close" type="button" aria-label="关闭" title="关闭窗口" @click="run('close')">
        <svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 3l6 6M9 3l-6 6" /></svg>
      </button>
    </div>

    <!-- 对话页：左侧用轨道标志 + PERSONAL AGENT；其他页保持原来克制的 QIO 字标。
         pointer-events 关掉，点在品牌区上仍可拖窗口。 -->
    <div class="brand" aria-hidden="true">
      <template v-if="isConversation">
        <img class="brand-logo" :src="brandLogo" alt="" />
        <span class="brand-sep"></span>
        <span class="brand-caption">PERSONAL AGENT</span>
      </template>
      <template v-else>
        <span class="brand-dot"></span>
        <span class="brand-name">QIO</span>
      </template>
    </div>

    <!-- 设置入口：只在对话页出现，固定排在窗口按钮左边（最右仍是窗口控制）。 -->
    <div v-if="isConversation" class="chrome-actions" :class="{ 'with-controls': inShell }">
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

/* ---- 设置入口（只在对话页出现） ---- */
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

/* ---- 对话页：整条全局栏是一张圆角卡片（与下方工作区卡片同一条视觉基线） ---- */
/*
 * 其他页面保持原来的 32px 透明标题条；只有对话页把窗口层变成 62px 高的可见全局栏。
 * 这样设置入口、窗口控制在 Tauri 里仍然可点，浏览器里只留设置入口。
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
</style>
