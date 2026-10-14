/**
 * 横向溢出 / 可横向浏览的**测量口径**（唯一实现，供组件自检与复核读取）。
 *
 * 为什么需要它：验收里「不横向滚动」不能用「源码写了 overflow: hidden / clip」来证明 ——
 * 那可能只是「藏起滚动条，内容还在左右滚」，也可能把内容直接裁掉（不可达）。
 * 判定必须看**实际渲染结果**，而且是两项**缺一不可**：
 *
 *   1. 内容溢出：scrollWidth - clientWidth > 1px（1px 取整误差不算溢出）；
 *   2. 真的能移动：给 scrollLeft 一个增量后读回，值真的变了才算「可以横向浏览」。
 *
 * 两两组合的含义：
 *   - 溢出 + 能移动 = 横向浏览区（独立材料预览里按材料类型允许）；
 *   - 溢出 + 不能移动 = 内容被裁掉 / 不可达（必须判为不通过）；
 *   - 不溢出        = 无论能不能移动都不构成横向滚动。
 *
 * 只读测量：scrollLeft 的探测会**原样还原**，不改变用户看到的滚动位置。
 */

/** 1px 取整误差不算「溢出」。 */
export const H_OVERFLOW_EPS = 1;

export interface HorizontalFit {
  scrollWidth: number;
  clientWidth: number;
  /** 溢出像素（< 0 按 0 算） */
  overflowPx: number;
  overflowing: boolean;
  /** scrollLeft 真的能移动（只有溢出时才探测） */
  canScroll: boolean;
  /** = overflowing && canScroll：这个区域现在真的可以横向浏览 */
  scrollable: boolean;
}

export function measureHorizontalFit(el: HTMLElement | null | undefined): HorizontalFit | null {
  if (!el || typeof el !== "object") return null;
  const scrollWidth = Number(el.scrollWidth) || 0;
  const clientWidth = Number(el.clientWidth) || 0;
  const overflowPx = Math.max(0, scrollWidth - clientWidth);
  const overflowing = overflowPx > H_OVERFLOW_EPS;
  let canScroll = false;
  if (overflowing) {
    const before = Number(el.scrollLeft) || 0;
    try {
      el.scrollLeft = before + 1;
      canScroll = (Number(el.scrollLeft) || 0) !== before;
    } catch {
      // 不是滚动容器 / 只读实现：就是动不了
      canScroll = false;
    } finally {
      try {
        el.scrollLeft = before;
      } catch {
        /* 还原失败就算了：这不是可写容器 */
      }
    }
  }
  return { scrollWidth, clientWidth, overflowPx, overflowing, canScroll, scrollable: overflowing && canScroll };
}

export interface HorizontalFitSample extends HorizontalFit {
  /** 稳定标识（给复核与测试读的 key，不用类名） */
  key: string;
  el: HTMLElement;
}

export interface HorizontalFitReport {
  context: "material" | "chat";
  /** 外层：卡片 / 卡片正文 / 预览正文 / 预览根 —— 一律不允许横向溢出 */
  outer: HorizontalFitSample[];
  /** 材料查看区（[data-qio-pan]）：chat 里不许溢出；material 里溢出就必须真的能移动 */
  viewers: HorizontalFitSample[];
  /** 外层溢出的 key（有值即不通过） */
  outerOverflow: string[];
  /** 溢出但 scrollLeft 动不了 = 内容被裁掉 / 不可达（有值即不通过） */
  clipped: string[];
  /** 真的能横向浏览的区（material 下这是合法能力，chat 下应当为空） */
  pannable: string[];
  ok: boolean;
}

function sample(key: string, el: Element | null): HorizontalFitSample | null {
  if (!el) return null;
  const measured = measureHorizontalFit(el as HTMLElement);
  return measured ? { key, el: el as HTMLElement, ...measured } : null;
}

/**
 * 对一个预览根节点做一次横向自检。
 *
 * @param root    预览根（CardInlinePreview 的 .cip）
 * @param context 渲染上下文：chat 要求查看区也不溢出；material 允许查看区二维浏览
 */
export function checkHorizontalFit(
  root: HTMLElement | null | undefined,
  context: "material" | "chat",
): HorizontalFitReport | null {
  if (!root || typeof root.querySelectorAll !== "function") return null;
  const outerCandidates: Array<[string, Element | null]> = [
    ["card", typeof root.closest === "function" ? root.closest('[data-im="card"]') : null],
    ["card-body", typeof root.closest === "function" ? root.closest('[data-im="card-body"]') : null],
    ["cip-body", root.querySelector(".cip-body")],
    ["cip", root],
  ];
  const outer: HorizontalFitSample[] = [];
  for (const [key, el] of outerCandidates) {
    const item = sample(key, el);
    if (item) outer.push(item);
  }
  const viewers: HorizontalFitSample[] = [];
  root.querySelectorAll("[data-qio-pan]").forEach((el, index) => {
    const item = sample("viewer:" + (el.getAttribute("data-qio-pan") || "?") + ":" + index, el);
    if (item) viewers.push(item);
  });

  const outerOverflow = outer.filter((item) => item.overflowing).map((item) => item.key);
  const clipped = viewers.filter((item) => item.overflowing && !item.canScroll).map((item) => item.key);
  const pannable = viewers.filter((item) => item.scrollable).map((item) => item.key);
  const chatOverflow = context === "chat" ? viewers.filter((item) => item.overflowing).map((item) => item.key) : [];
  return {
    context,
    outer,
    viewers,
    outerOverflow,
    clipped,
    pannable,
    ok: outerOverflow.length === 0 && clipped.length === 0 && chatOverflow.length === 0,
  };
}
