/**
 * 输入区尺寸的**唯一量测**。
 *
 * 为什么需要：同一份「输入框有多高、上缘在哪」以前被量了三遍 ——
 * 消息流的底部滚动缓冲、底部内容块的让位内边距、以及「回到最新消息」按钮的锚定，
 * 各自 `querySelector(".composer")` + `getBoundingClientRect()`。同一帧里读三次布局、
 * 三个观察器分别挂着，既是重复计算，也会各自抖动。
 *
 * 现在只保留一个 `ResizeObserver` 与一组订阅者：
 * - 订阅时**同步**量一次（首帧就有正确值，不必等到下一帧）；
 * - 尺寸/视口变化合并到同一帧通知一次；
 * - 最后一个订阅者退订时，观察器与监听全部拆除（不留下常驻监听）。
 */
export interface ComposerMetrics {
  /** 页面上有没有输入面板（没有就优雅降级，不要编造尺寸） */
  found: boolean;
  /** 整个输入面板的高度（px）：消息流底部缓冲与底部内容块让位都用它 */
  height: number;
  /** 输入面板上缘的视口坐标（px） */
  top: number;
  /**
   * **输入框本体**上缘的视口坐标（px）：面板里那条带占位符的圆角框，
   * 不含它上面的话题名 / 提示行。「回到最新消息」按钮要贴着它，不是贴着整个面板。
   */
  inputTop: number;
  /** 输入面板中线的视口坐标（px）：与对话内容列同一条列，用作水平对齐的兜底 */
  centerX: number;
  /**
   * 局部像素 → 视口像素的比例（CSS `zoom` 场景）。
   * `getBoundingClientRect()` 给的是视口像素，而 `position: fixed` 的 `top/left/bottom` 写在
   * 缩放子树里是**局部像素**；不换算的话页面缩放后位置会整体偏掉（实测 zoom=0.8 时
   * 按钮跑到输入区里面去，间距 -14.4px）。正常情况恒为 1。
   */
  scale: number;
}

const EMPTY: ComposerMetrics = {
  found: false,
  height: 0,
  top: 0,
  inputTop: 0,
  centerX: 0,
  scale: 1,
};
const subscribers = new Set<(metrics: ComposerMetrics) => void>();

let observer: ResizeObserver | null = null;
let raf = 0;
let tries = 0;
let listening = false;
/** 输入框可能比订阅者晚挂载：有限次重试（有终止条件） */
const MAX_OBSERVE_TRIES = 60;

function readComposer(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".composer");
}

/** 同步量一次（拿不到输入面板时明确回 found=false，调用方据此优雅降级） */
export function measureComposer(): ComposerMetrics {
  const panel = readComposer();
  if (!panel) return { ...EMPTY };
  const rect = panel.getBoundingClientRect();
  // 找不到 textarea 就不再读第二次布局：退回面板上缘
  const inputEl = panel.querySelector<HTMLElement>("textarea");
  const inputTop = inputEl ? inputEl.getBoundingClientRect().top || rect.top : rect.top;
  // 局部像素 → 视口像素：offsetHeight 是布局像素，rect.height 是视口像素
  const scale = panel.offsetHeight > 0 ? rect.height / panel.offsetHeight : 1;
  return {
    found: true,
    height: rect.height,
    top: rect.top,
    inputTop,
    centerX: rect.left + rect.width / 2,
    scale: scale > 0 ? scale : 1,
  };
}

function notify(): void {
  raf = 0;
  const metrics = measureComposer();
  for (const cb of [...subscribers]) cb(metrics);
}

/** 同一帧内多次触发只通知一次 */
function schedule(): void {
  if (raf) return;
  raf = requestAnimationFrame(notify);
}

function ensureObserver(): void {
  if (observer || typeof ResizeObserver === "undefined") return;
  const target = readComposer();
  if (!target) {
    if (tries >= MAX_OBSERVE_TRIES) return;
    tries += 1;
    requestAnimationFrame(ensureObserver);
    return;
  }
  observer = new ResizeObserver(schedule);
  observer.observe(target);
}

function startListening(): void {
  if (listening) return;
  listening = true;
  window.addEventListener("resize", schedule);
  window.visualViewport?.addEventListener("resize", schedule);
  ensureObserver();
}

function stopListening(): void {
  if (!listening) return;
  listening = false;
  window.removeEventListener("resize", schedule);
  window.visualViewport?.removeEventListener("resize", schedule);
  observer?.disconnect();
  observer = null;
  tries = 0;
  if (raf) cancelAnimationFrame(raf);
  raf = 0;
}

/**
 * 订阅输入区尺寸。返回退订函数 —— **组件卸载时必须调用**（否则监听会留到进程结束）。
 */
export function subscribeComposerMetrics(
  cb: (metrics: ComposerMetrics) => void,
): () => void {
  subscribers.add(cb);
  startListening();
  // 同步先给一次：首屏（例如已经有候选卡）不能等下一帧才让位
  cb(measureComposer());
  return () => {
    subscribers.delete(cb);
    if (!subscribers.size) stopListening();
  };
}

/** 供测试或特殊时机强制刷新一次（合并到下一帧） */
export function refreshComposerMetrics(): void {
  schedule();
}
