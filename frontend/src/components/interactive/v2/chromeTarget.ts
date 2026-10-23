/**
 * 找到 A 在 WindowChrome 里提供的互动页挂载点（契约 §2.1：#qio-interactive-chrome-extra）。
 *
 * 为什么不能直接 Teleport：
 * - 容器可能比本组件晚一帧出现（路由刚切到互动页），也可能还没合并 A 的改动；
 * - Vue 的 Teleport 在目标不存在时只会告警并不渲染，之后目标出现也不会自动接上。
 * 所以这里用 MutationObserver 等它出现；找不到时调用方不渲染任何东西 ——
 * 宁可没有控件，也不自己造一条假顶栏。
 */
import { onBeforeUnmount, onMounted, ref, watch, type Ref } from "vue";

function resolveElement(selector: string): HTMLElement | null {
  if (typeof document === "undefined") return null;
  const found = document.querySelector(selector);
  return found instanceof HTMLElement ? found : null;
}

/**
 * @param selector 挂载点选择器，或返回选择器的函数（跟随 props 变化时用函数）。
 * @returns 目标元素；没出现时为 null。
 */
export function useChromeExtraTarget(
  selector: string | (() => string),
): Ref<HTMLElement | null> {
  const target = ref<HTMLElement | null>(null);
  let observer: MutationObserver | null = null;

  function readSelector(): string {
    return typeof selector === "function" ? selector() : selector;
  }

  function resolve(): void {
    const next = resolveElement(readSelector());
    if (target.value !== next) target.value = next;
  }

  onMounted(() => {
    resolve();
    if (typeof MutationObserver === "undefined" || typeof document === "undefined") return;
    observer = new MutationObserver(() => resolve());
    observer.observe(document.documentElement, { childList: true, subtree: true });
  });

  watch(() => readSelector(), () => resolve());

  onBeforeUnmount(() => {
    observer?.disconnect();
    observer = null;
  });

  return target;
}
