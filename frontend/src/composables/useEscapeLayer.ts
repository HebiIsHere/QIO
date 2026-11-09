/**
 * 统一 Esc 层级栈（共享小工具，2026-11 C 组新增；D 组与其它中央弹层可直接复用）。
 *
 * 解决的是什么：多个界面各自在 window 上挂「捕获阶段」Esc 监听时，一次 Esc 会
 * 按监听注册顺序全部执行 —— 外层弹窗的捕获监听通常先注册、先执行，内层下拉
 * 再 `stopPropagation` 也来不及（外层已经关掉了）。同一类问题还包括：
 * 全窗口预览和前方弹窗各自监听 Esc，一次 Esc 同时关两层。
 *
 * 规则（唯一仲裁）：
 * - 可关闭界面在**打开时**注册一层，关闭时注销；后注册的内层优先；
 * - 一次 Esc 只交给「最后注册且当前 active」的那一层；该层返回 true = 已处理，
 *   阻止默认行为并停止继续冒泡，更外层不会再收到；
 * - 该层返回 false = 这一层不处理，继续交给更外层（例如下拉菜单没有可选项时）；
 * - 没有任何层处理时什么都不做，把按键留给页面既有逻辑；
 * - 输入法组合输入（isComposing / keyCode 229）期间 Esc 属于「取消选字」，
 *   不当作关闭界面，也不能吃掉（否则输入法无法取消候选）。
 *
 * 用法（QCombo / QSelect / QModal 已接入）：
 * ```ts
 * useEscapeLayer({
 *   id: "my-layer-" + instanceId,
 *   active: () => open.value,          // 打开时才参与层级
 *   onEscape: () => { close(); return true; },
 * });
 * ```
 *
 * 注意：不要再用 `window.addEventListener("keydown", ..., true)` 自己处理 Esc，
 * 否则会绕过这个层级栈、和本工具抢先后（QModal 的 Tab 焦点陷阱不受影响：Tab 不走这里）。
 */
import { onBeforeUnmount } from "vue";

/** 一层 Esc 处理器。 */
export interface EscapeLayerOptions {
  /** 稳定身份（调试 / 断言用；同一 id 重复注册以最新一次为准，不会叠加）。 */
  id: string;
  /** 这一层当前是否参与处理（默认一直参与）。关闭时返回 false。 */
  active?: () => boolean;
  /**
   * 处理 Esc。返回 true 表示这一层已经处理（阻止默认行为并停止继续冒泡）；
   * 返回 false 表示这一层不处理，交给更外层。
   */
  onEscape: (event: KeyboardEvent) => boolean;
}

interface RegisteredEscapeLayer extends EscapeLayerOptions {
  seq: number;
}

const layers: RegisteredEscapeLayer[] = [];
let seq = 0;
let installed = false;

/**
 * 是不是输入法组合输入阶段（选字）产生的键盘事件。
 * - `event.isComposing`：标准字段，Chrome / Edge / Firefox / Safari 现代版本都有；
 * - `keyCode === 229`：部分浏览器/输入法在组合输入期间的兜底标记。
 * 这两个成立时不能当作界面操作（回车确认候选字 ≠ 选中下拉项）。
 */
export function isComposingKeyEvent(event: KeyboardEvent): boolean {
  return Boolean(event.isComposing) || event.keyCode === 229;
}

function dispatchEscape(event: KeyboardEvent): void {
  if (event.key !== "Escape" || event.defaultPrevented) return;
  if (isComposingKeyEvent(event)) return;
  for (let index = layers.length - 1; index >= 0; index -= 1) {
    const layer = layers[index];
    if (!layer) continue;
    if (layer.active && !layer.active()) continue;
    const handled = layer.onEscape(event);
    if (handled === false) continue;
    event.preventDefault();
    event.stopPropagation();
    return;
  }
}

function install(): void {
  if (installed || typeof window === "undefined") return;
  installed = true;
  window.addEventListener("keydown", dispatchEscape, true);
}

function uninstallIfEmpty(): void {
  if (!installed || layers.length > 0 || typeof window === "undefined") return;
  installed = false;
  window.removeEventListener("keydown", dispatchEscape, true);
}

function removeLayerId(id: string): void {
  for (let index = layers.length - 1; index >= 0; index -= 1) {
    if (layers[index]?.id === id) layers.splice(index, 1);
  }
}

/**
 * 注册一层 Esc 处理器，返回注销函数（可重复调用）。
 * 一般在可关闭界面打开时调用；组件内建议用 `useEscapeLayer` 自动随卸载注销。
 */
export function registerEscapeLayer(options: EscapeLayerOptions): () => void {
  if (!options || !options.id) return () => {};
  removeLayerId(options.id);
  const layer: RegisteredEscapeLayer = { ...options, seq: (seq += 1) };
  layers.push(layer);
  install();
  let disposed = false;
  return () => {
    if (disposed) return;
    disposed = true;
    const index = layers.indexOf(layer);
    if (index >= 0) layers.splice(index, 1);
    uninstallIfEmpty();
  };
}

/** 组件内注册一层 Esc 处理器：setup 时登记，组件卸载时自动注销。 */
export function useEscapeLayer(options: EscapeLayerOptions): void {
  const dispose = registerEscapeLayer(options);
  onBeforeUnmount(dispose);
}
