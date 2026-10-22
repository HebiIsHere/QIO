<script setup lang="ts">
/**
 * 自定义标题条 —— 无边框窗口（tauri.conf.json: `decorations: false`）的「页眉」。
 *
 * 为什么需要它：去掉系统标题栏之后，窗口既没有可拖拽的区域，也没有最小化/
 * 最大化/关闭按钮。这两件事必须由前端补上，否则用户只能靠 Alt+F4 关窗口。
 *
 * 设计上的克制：
 * - 条内不放标题文字，只做拖拽面 + 三个按钮 —— 用户要的就是「没有页眉」；
 * - 按钮常驻但只用 muted 色，hover 才亮，关闭按钮才出现危险色；
 * - 条本身透明：星球页、对话页的底色直接透上来，不额外压一层色块。
 *
 * 拖拽面与按钮是**兄弟节点**而不是父子：Tauri 的拖拽区按命中元素判定，
 * 按钮若在拖拽面内部，点按钮就会先被拖拽逻辑截走（不同版本判定 `hasAttribute`
 * 或 `closest` 的行为还不一致），拆开两种判定都安全。
 *
 * R9 §11（W5）：关闭按钮不再直接 `window.close()`。标题条关闭、系统
 * CloseRequested、Alt+F4 全部走同一个 `handleCloseRequest`：
 * ask（默认）→ 确认框；tray → 壳隐藏窗口继续运行；quit → 真正退出流程。
 * 托盘菜单「退出 QIO」由壳发 `qio://tray-quit` 事件，也走同一个退出流程。
 */
import { onBeforeUnmount, onMounted, ref } from "vue";
import { useUiStore } from "../stores/ui";
import {
  CLOSE_BEHAVIOR_OPTIONS,
  DEFAULT_CLOSE_BEHAVIOR,
  type CloseBehavior,
} from "../services/quitGuard";
import {
  inTauriShell,
  listenShellEvent,
  requestClose,
  startQuitFlow,
} from "../services/desktop";

/** @tauri-apps/api/window 里我们真正用到的那几个方法（只声明用到的部分，便于测试替身）。 */
interface ShellWindow {
  minimize(): Promise<void>;
  toggleMaximize(): Promise<void>;
  isMaximized(): Promise<boolean>;
  onResized(handler: () => void): Promise<() => void>;
}

function tauriInternals(): unknown {
  return (globalThis as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
}

/** 是否在 Tauri 壳里。浏览器开发预览（IAB）里没有它，此时不画按钮：
 *  画了也点不动 —— 那正是「看起来能用、其实不能用」。 */
const inShell = ref(false);
const maximized = ref(false);
const ui = useUiStore();

/** 关闭确认框（仅在关闭行为 = ask 时出现）。 */
const askOpen = ref(false);
const rememberChoice = ref(false);
const busy = ref(false);
const notice = ref("");

let shell: ShellWindow | null = null;
let unlisten: (() => void) | null = null;
let unlistenClose: (() => void) | null = null;
let unlistenTrayQuit: (() => void) | null = null;
let disposed = false;
/** 防止「标题条点击 + 系统关闭事件」在同一瞬间进入两次（弹两个框 / 退两次）。 */
let handlingClose = false;

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
    console.error(`[qio] 窗口操作 ${action} 失败`, err);
  }
}

function currentBehavior(): CloseBehavior {
  // 设置可能还没加载（启动阶段就按了关闭）：默认 ask 是契约默认，也是唯一不会
  // 静默杀掉/藏起来的选项。loaded 之后 store 里就是后端的最新值。
  if (!ui.loaded) return DEFAULT_CLOSE_BEHAVIOR;
  return ui.closeBehavior;
}

async function ensureBehaviorLoaded() {
  // 每次都从后端重读一次：设置可能在别处/上一个会话改过（真机验证脚本就是用 API 设置的），
  // 用启动时的缓存会让「改了设置但这次关闭仍按旧行为」——那正是最不该出现的不确定性。
  // load() 自己会吞掉错误并置 loaded：后端未就绪时回退到 ask（先问用户）。
  await ui.load();
}

/** 最小化到托盘：只隐藏窗口，不触发退出收尾，后端继续运行。 */
async function hideToTray() {
  try {
    await requestClose("tray");
    notice.value = "";
  } catch (err) {
    console.error("[qio] 隐藏到托盘失败", err);
    notice.value = "无法隐藏到系统托盘（详见日志）";
  }
}

/** 真正退出流程：前端守卫 → 后端有界收尾 → 壳按 pid 等待/兜底并退出。 */
async function quitNow() {
  busy.value = true;
  notice.value = "";
  askOpen.value = false;
  const result = await startQuitFlow();
  if (!result.ok) {
    // 壳命令失败（进程还在）：如实报错并让用户还能继续操作，不假装已退出。
    busy.value = false;
    notice.value = result.detail;
  }
}

/** 统一关闭入口（契约 §11）。 */
async function handleCloseRequest() {
  if (handlingClose || busy.value) return;
  handlingClose = true;
  try {
    await ensureBehaviorLoaded();
    const behavior = currentBehavior();
    if (behavior === "tray") {
      await hideToTray();
      return;
    }
    if (behavior === "quit") {
      await quitNow();
      return;
    }
    // ask：先经过壳的统一入口（幂等/退出互斥由壳保证），再弹 QIO 自己的确认框。
    let outcome = null;
    try {
      outcome = await requestClose("ask");
    } catch (err) {
      // 壳不可用（理论上只会在浏览器预览里）：保持「询问」的默认行为。
      console.error("[qio] 关闭入口不可用", err);
    }
    if (outcome?.quitting) return;
    askOpen.value = true;
  } finally {
    handlingClose = false;
  }
}

async function chooseClose(action: "tray" | "quit") {
  if (busy.value) return;
  askOpen.value = false;
  if (rememberChoice.value) {
    try {
      await ui.setCloseBehavior(action);
    } catch (err) {
      // 记忆失败不影响本次选择，但要留下可查原因（下次还会问）。
      console.error("[qio] 保存关闭行为失败", err);
    }
  }
  rememberChoice.value = false;
  if (action === "tray") await hideToTray();
  else await quitNow();
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
    // 系统关闭（Alt+F4 / 任务栏关闭 / WM_CLOSE）由 Rust 拦下并发这个事件：
    // 和标题条按钮走完全相同的关闭行为判断。
    unlistenClose = await listenShellEvent("qio://close-requested", () => {
      void handleCloseRequest();
    });
    // 托盘菜单「退出 QIO」：用户已明确选择退出，不再弹 ask 确认框。
    unlistenTrayQuit = await listenShellEvent("qio://tray-quit", () => {
      void quitNow();
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
});
</script>

<template>
  <div class="window-chrome">
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
      <!-- R9 §11：不再直接 close()，而是走关闭行为（ask/tray/quit） -->
      <button class="wc-btn wc-close" type="button" aria-label="关闭" title="关闭窗口" @click="handleCloseRequest">
        <svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 3l6 6M9 3l-6 6" /></svg>
      </button>
    </div>

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
            记住我的选择（以后按「{{ behaviorLabel(currentBehavior()) }}」处理）
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
