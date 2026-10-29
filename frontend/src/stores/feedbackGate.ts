/**
 * 自动弹出 / 通知显示的前置门控（B）。
 *
 * 依据用户任务书与契约 §3.5：审批与通知**不得打断输入**，也不在设置页 / 记忆星球覆盖层上
 * 自动弹出。这里只做真实的「此刻能不能自动出现」判断，不改变任何业务状态：
 *
 * 1. 输入法组合输入（composition）期间不弹；正在输入框里也不弹；
 * 2. 离开输入框后**短暂延迟**再试（集中常量 INPUT_LEAVE_RETRY_DELAY_MS），
 *    避免「刚按完回车就弹窗」；
 * 3. 有中央弹窗（modalArbiter）或被关闭询问整体挂起时不弹；
 * 4. 记忆星球是**覆盖层**（路由可能还是对话页）：按真实覆盖层判断，不能只看路由名；
 * 5. 设置页 / 调试页不弹；页面完全没渲染（单元测试 / 启动早期）时不做额外阻挡。
 *
 * 监听器只由 installFeedbackGate() 安装一次（GlobalFeedbackHost 负责），
 * 卸载时必须调用返回的清理函数，避免重复订阅与泄漏。
 */
import { reactive } from "vue";
import router from "../router";
import { planetContinuum } from "../composables/planetContinuum";
import { useModalArbiterStore } from "./modalArbiter";

/** 离开输入框后到允许自动弹出的延迟（120–250ms 区间的集中常量）。 */
export const INPUT_LEAVE_RETRY_DELAY_MS = 180;

export type AutoOpenBlockReason =
  | "ok"
  | "editing"
  | "composing"
  | "input-just-left"
  | "modal"
  | "planet-overlay"
  | "no-chat-surface"
  | "document-hidden";

export interface AutoOpenDecision {
  ok: boolean;
  reason: AutoOpenBlockReason;
  /** 条件恢复后是否值得再试（永久性阻挡返回 false，调用方不要留 pending 标记）。 */
  retry: boolean;
}

export const feedbackGate = reactive({
  /** 当前焦点是否在输入框 / 文本域 / 可编辑区域。 */
  editing: false,
  /** 是否正在输入法组合输入（compositionstart 到 compositionend 之间）。 */
  composing: false,
  /** 最近一次**离开**输入框的时间；0 = 本轮没有离开过。 */
  lastInputLeftAt: 0,
  installed: false,
});

const CHAT_SURFACE_SELECTORS = [".conversation-workspace", ".interactive"];
const SETTINGS_SURFACE_SELECTORS = [".settings"];
const DEBUG_SURFACE_SELECTORS = [".debug"];
const PLANET_OVERLAY_SELECTOR = ".planet-view";

export type VisibleSurface = "chat" | "planet" | "settings" | "debug" | "unknown";

function isEditableElement(el: Element | null | undefined): boolean {
  if (!el) return false;
  const tag = (el as HTMLElement).tagName;
  if (tag === "INPUT" || tag === "TEXTAREA") return true;
  return (el as HTMLElement).isContentEditable === true;
}

/** 元素是否真实可见（不用 getBoundingClientRect：jsdom 下恒为 0）。 */
function isElementVisible(el: Element | null | undefined): boolean {
  if (!el || typeof document === "undefined") return false;
  const style = globalThis.getComputedStyle?.(el as Element);
  if (!style) return true;
  if (style.display === "none") return false;
  if (style.visibility === "hidden" || style.visibility === "collapse") return false;
  if (style.opacity && Number(style.opacity) === 0) return false;
  return true;
}

function anyVisible(selectors: readonly string[]): boolean {
  if (typeof document === "undefined") return false;
  for (const selector of selectors) {
    const el = document.querySelector(selector);
    if (el && isElementVisible(el)) return true;
  }
  return false;
}

/** 星球入口小球态的两个类：此时 `.planet-view` 只是那颗常驻小球，不是覆盖层。 */
const PLANET_ENTRY_CLASSES = ["ball", "tail"];

/**
 * 记忆星球**覆盖层**是否开着（不是「星球组件是否挂载」）。
 *
 * 真实基线（2026-10-27 核对）：对话页空闲时 PlanetView 也常驻在 DOM 里，
 * `open=false` 时根元素是 `.planet-view.ball` / `.tail`（入口小球，opacity 1），
 * 它**不是**覆盖层 —— 只按选择器判断会让审批/通知永远不自动弹。
 *
 * 判定顺序（真实状态优先，不只凭路由名）：
 * 1. 连续体阶段非 idle：正在展开/已就绪/正在收起 → 覆盖层在屏幕上；
 * 2. 没有阶段（直接挂载 / 单测）：元素可见且不带 ball/tail 入口态类 → 展开态覆盖层；
 * 3. 其余（小球态）→ 不是覆盖层。
 */
export function isPlanetOverlayOpen(): boolean {
  if (planetContinuum.phase !== "idle") return true;
  if (typeof document === "undefined") return false;
  const el = document.querySelector(PLANET_OVERLAY_SELECTOR) as HTMLElement | null;
  if (!el || !isElementVisible(el)) return false;
  return !PLANET_ENTRY_CLASSES.some((name) => el.classList.contains(name));
}

/**
 * 当前真实可见页面（DOM 优先，路由名只作为「页面还没渲染时」的兜底）。
 *
 * 关键：记忆星球是覆盖层，打开时对话页仍然在 DOM 里 ——
 * 所以星球必须**先于**对话页判断。
 */
export function visibleSurface(): VisibleSurface {
  if (isPlanetOverlayOpen()) return "planet";
  if (anyVisible(CHAT_SURFACE_SELECTORS)) return "chat";
  if (anyVisible(SETTINGS_SURFACE_SELECTORS)) return "settings";
  if (anyVisible(DEBUG_SURFACE_SELECTORS)) return "debug";
  const name = router.currentRoute.value?.name;
  if (name === "conversation" || name === "interactive") return "chat";
  if (name === "settings") return "settings";
  if (name === "debug") return "debug";
  return "unknown";
}

/**
 * 用户此刻是否在输入（编辑 + 输入法组合）。
 *
 * 除了监听器维护的状态，再实时看一次 activeElement：测试 / 极早期没有安装监听器时，
 * 「焦点就在输入框里」这个事实也必须挡住自动弹出（不能只凭一次 blur 判断）。
 */
export function isInputBusy(): boolean {
  if (feedbackGate.composing) return true;
  if (feedbackGate.editing) return true;
  if (typeof document === "undefined") return false;
  return isEditableElement(document.activeElement);
}

/**
 * 是否有**别的**中央弹窗在 DOM 里（兼容尚未接入 arbiter 的旧弹窗）。
 *
 * 统一审批弹窗自己带 data-unified-approval-modal，不算「别的」。
 * 新组件应当用 modalArbiter 注册；这里是最后一道保护，不当主路径。
 */
function hasBlockingDialogDom(): boolean {
  if (typeof document === "undefined") return false;
  const nodes = Array.from(
    document.querySelectorAll('[role="dialog"][aria-modal="true"], dialog[open]'),
  );
  return nodes.some((node) => !node.hasAttribute("data-unified-approval-modal"));
}

/**
 * 现在能不能自动弹出审批 / 通知。
 *
 * 返回原因与是否值得稍后再试：调用方据此决定「留 pending 标记等条件恢复」还是放弃。
 */
export function canAutoOpenApproval(now: number = Date.now()): AutoOpenDecision {
  if (typeof document !== "undefined" && document.visibilityState === "hidden") {
    return { ok: false, reason: "document-hidden", retry: true };
  }
  if (feedbackGate.composing) return { ok: false, reason: "composing", retry: true };
  if (isInputBusy()) return { ok: false, reason: "editing", retry: true };
  if (
    feedbackGate.lastInputLeftAt &&
    now - feedbackGate.lastInputLeftAt < INPUT_LEAVE_RETRY_DELAY_MS
  ) {
    return { ok: false, reason: "input-just-left", retry: true };
  }
  // 注意：这里只读 arbiter，不要求审批自己注册；审批弹窗已打开时 maybeAutoOpen 会先返回。
  let blocks = false;
  try {
    blocks = useModalArbiterStore().blocksAutoApproval;
  } catch {
    // store 尚未初始化（极早期 / 单测）：不因这个判断把功能挡死
    blocks = false;
  }
  if (blocks || hasBlockingDialogDom()) return { ok: false, reason: "modal", retry: true };
  const surface = visibleSurface();
  if (surface === "planet") return { ok: false, reason: "planet-overlay", retry: true };
  if (surface === "settings" || surface === "debug") {
    return { ok: false, reason: "no-chat-surface", retry: false };
  }
  return { ok: true, reason: "ok", retry: false };
}

// ── 输入活动监听：只由 GlobalFeedbackHost 安装一次 ───────────────────────────

type Listener = () => void;
const inputLeftListeners = new Set<Listener>();

/** 订阅「用户完全离开输入区域」事件（返回取消订阅函数）。 */
export function subscribeInputLeft(listener: Listener): () => void {
  if (!listener) return () => undefined;
  inputLeftListeners.add(listener);
  return () => inputLeftListeners.delete(listener);
}

function notifyInputLeft(): void {
  for (const listener of [...inputLeftListeners]) {
    try {
      listener();
    } catch {
      // 订阅方异常不影响输入本身
    }
  }
}

/**
 * 安装输入活动监听（focusin / focusout / compositionstart / compositionend）。
 *
 * @returns 清理函数：卸载/换页时必须调用（幂等；重复安装只生效一次）。
 */
export function installFeedbackGate(target?: Document): () => void {
  if (typeof document === "undefined") return () => undefined;
  const doc = target ?? document;
  if (feedbackGate.installed) return () => undefined;
  feedbackGate.installed = true;

  const markEditing = (): void => {
    const next = isEditableElement(doc.activeElement);
    if (feedbackGate.editing && !next) {
      feedbackGate.lastInputLeftAt = Date.now();
      notifyInputLeft();
    }
    feedbackGate.editing = next;
    if (next) feedbackGate.lastInputLeftAt = 0;
  };

  const onFocusIn = (): void => {
    markEditing();
  };
  const onFocusOut = (event: Event): void => {
    const next = (event as FocusEvent).relatedTarget as Element | null;
    if (isEditableElement(next) || isEditableElement(doc.activeElement)) {
      // 焦点只是从一个输入区移到另一个输入区：仍然在输入，不触发「离开」
      feedbackGate.editing = true;
      return;
    }
    markEditing();
    if (!feedbackGate.editing) {
      feedbackGate.lastInputLeftAt = Date.now();
      notifyInputLeft();
    }
  };
  const onCompositionStart = (event: Event): void => {
    if (!isEditableElement(event.target as Element | null) && !feedbackGate.editing) return;
    feedbackGate.composing = true;
  };
  const onCompositionEnd = (): void => {
    feedbackGate.composing = false;
  };

  doc.addEventListener("focusin", onFocusIn, true);
  doc.addEventListener("focusout", onFocusOut, true);
  doc.addEventListener("compositionstart", onCompositionStart, true);
  doc.addEventListener("compositionend", onCompositionEnd, true);

  // 初始化一次：安装时焦点可能已经在输入框里
  feedbackGate.editing = isEditableElement(doc.activeElement);

  return () => {
    doc.removeEventListener("focusin", onFocusIn, true);
    doc.removeEventListener("focusout", onFocusOut, true);
    doc.removeEventListener("compositionstart", onCompositionStart, true);
    doc.removeEventListener("compositionend", onCompositionEnd, true);
    feedbackGate.installed = false;
    feedbackGate.editing = false;
    feedbackGate.composing = false;
    feedbackGate.lastInputLeftAt = 0;
  };
}

/** 测试 / 重连收口：把门控状态清到初始值（不卸载监听）。 */
export function resetFeedbackGateState(): void {
  feedbackGate.editing = false;
  feedbackGate.composing = false;
  feedbackGate.lastInputLeftAt = 0;
}