/**
 * 卡片局部工具栏「固定锚点」的 **jsdom 布局桩**（可复用测试工具，不依赖任何组件）。
 *
 * 为什么需要：jsdom 没有真实布局，`getBoundingClientRect()` 全返回 0，
 * 于是「工具栏是否贴在卡片左边缘 / 底边是否在卡片顶边上方 10px」这类断言
 * 在 jsdom 里根本量不到。这里给 Element 装上一个小型布局模型：
 *
 * 1. `Element.prototype.getBoundingClientRect` 全局替换，按下面几条真实浏览器语义算外框；
 * 2. 容器（.board-shell/.board-main/.board-viewport/.card-toolbar-host）与板面可视区同盒；
 * 3. `.board-scroll-content` = 可视区原点 − 真实 scrollLeft/Top；
 * 4. `.board-surface` 按组件自己写的行内 `transform: scale()` 缩放（事实来源是组件，不是测试猜测）；
 * 5. 卡片 / 工具栏的屏幕外框 = 其 **containing block（最近的非 static 祖先）** 的原点
 *    + 行内 left/top × （该祖先及其祖先的 scale 乘积）；
 * 6. offsetWidth / offsetHeight / clientWidth / clientHeight 返回「布局自然尺寸」
 *    （浏览器语义：不含祖先 transform），scrollWidth / scrollHeight 返回内容尺寸，
 *    scrollLeft / scrollTop 可写 —— 组件写进去的滚动量就是事实。
 *
 * 于是测试可以直接断言：工具栏屏幕 left 与卡片屏幕 left 的差、卡片顶边到工具栏底边的距离
 * —— 与真实浏览器同一套几何口径（真实浏览器对照见 D:\\qio-dev\\_tb-evidence 下的探针）。
 *
 * 注意：containing block 走的是「最近的非 static 祖先」，CSS 都靠 `injectContainerPositions()`
 * 显式写进行内样式模拟（jsdom 不跑 SFC 的 <style>），遇到未知的定位祖先会记进 `warnings`，
 * 让结构变化直接暴露而不是悄悄算错。
 */

export interface LayoutRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
  width: number;
  height: number;
}

export interface LayoutOptions {
  /** 板面可视区（= .board-main / .board-viewport / .board-shell / .card-toolbar-host 的盒子） */
  viewport: { left: number; top: number; width: number; height: number };
  /** 板面内容尺寸（板坐标，未缩放） */
  surfaceSize: { w: number; h: number };
  /** 工具栏自然尺寸（jsdom 量不到，必须由测试给出），默认 337.59×35.25（真实应用实测值） */
  toolbarSize?: { w: number; h: number };
  /** 初始滚动量（真实 scrollLeft / scrollTop） */
  scroll?: { x: number; y: number };
  /** 卡片自然尺寸兜底（按 data-card-id）；缺省读行内 width/height */
  cardNatural?: Record<string, { w: number; h: number }>;
  /** 折叠卡片行内 height 是 auto：用这个自然高度兜底 */
  foldedHeight?: number;
}

export interface ContainingBlock {
  selector: string;
  element: HTMLElement;
  scale: { x: number; y: number };
  origin: { left: number; top: number };
}

export interface ToolbarMeasurement {
  /** 工具栏元素（可能是 display:none 的 v-show 元素；不存在时为 null） */
  toolbar: HTMLElement | null;
  card: HTMLElement | null;
  cardId: string | null;
  cardRect: LayoutRect | null;
  toolbarRect: LayoutRect | null;
  /** 工具栏可见（display 不是 none / visibility 不是 hidden / opacity > 0） */
  toolbarVisible: boolean;
  /** 工具栏是否还被渲染在 .board-surface（缩放层）内部 —— 修复后应为 false */
  toolbarInsideSurface: boolean;
  toolbarParent: string | null;
  /** 浮层宿主几号卡（BoardCanvas 写在 data-owner-card-id 上） */
  hostOwnerId: string | null;
  hostVisible: string | null;
  /** 工具栏屏幕 left − 卡片屏幕 left；目标 |dx| ≤ 1 */
  dx: number | null;
  /** 卡片屏幕 top − 工具栏屏幕 bottom；目标 |gap − 10| ≤ 1 */
  gap: number | null;
  /** 卡片与板面可视区是否相交 */
  cardIntersectsViewport: boolean | null;
  scale: number;
  scroll: { x: number; y: number };
}

export interface CardToolbarHarness {
  readonly viewport: HTMLElement;
  readonly surface: HTMLElement;
  readonly scrollContent: HTMLElement | null;
  readonly host: HTMLElement | null;
  /** 组件当前渲染出来的 scale（读 .board-surface 的行内 transform） */
  scale(): number;
  scroll(): { x: number; y: number };
  setScroll(x: number, y: number): void;
  setViewport(rect: { left: number; top: number; width: number; height: number }): void;
  setToolbarSize(size: { w: number; h: number }): void;
  setCardNatural(cardId: string, size: { w: number; h: number }): void;
  rectOf(element: Element): LayoutRect;
  cardElement(cardId: string): HTMLElement | null;
  cardRect(cardId: string): LayoutRect | null;
  toolbarElement(): HTMLElement | null;
  toolbarRect(): LayoutRect | null;
  containingBlockOf(element: Element): ContainingBlock | null;
  measure(): ToolbarMeasurement;
  /** 未知的定位祖先 / 无法解释的结构（应当为空；非空说明结构变了、桩要更新） */
  warnings: string[];
  restore(): void;
}

const ZERO: LayoutRect = { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };

function px(value: string | null | undefined, fallback = 0): number {
  if (!value) return fallback;
  const parsed = Number.parseFloat(value.replace("px", ""));
  return Number.isFinite(parsed) ? parsed : fallback;
}

/** 解析行内 transform 里的 scale（matrix(a,b,c,d,e,f) / scale(x[,y])）。 */
export function parseScale(transform: string | null | undefined): { x: number; y: number } {
  if (!transform) return { x: 1, y: 1 };
  const matrix = /matrix\(([^)]+)\)/.exec(transform);
  if (matrix) {
    const parts = matrix[1].split(",").map((item) => Number.parseFloat(item));
    if (parts.length >= 4 && Number.isFinite(parts[0]) && Number.isFinite(parts[3])) return { x: parts[0], y: parts[3] };
  }
  const scale = /scale\(([^)]+)\)/.exec(transform);
  if (scale) {
    const parts = scale[1].split(",").map((item) => Number.parseFloat(item));
    const x = Number.isFinite(parts[0]) ? parts[0] : 1;
    const y = Number.isFinite(parts[1]) ? parts[1] : x;
    return { x, y };
  }
  return { x: 1, y: 1 };
}

function inlineTransform(element: Element): string {
  const inline = (element as HTMLElement).style?.transform;
  if (inline) return inline;
  try {
    return getComputedStyle(element).transform ?? "";
  } catch {
    return "";
  }
}

const KNOWN_CONTAINERS: Array<{ selector: string; kind: string }> = [
  { selector: ".board-surface", kind: "surface" },
  { selector: ".board-scroll-content", kind: "scroll-content" },
  { selector: ".board-viewport", kind: "viewport" },
  { selector: ".board-main", kind: "main" },
  { selector: ".board-shell", kind: "shell" },
  { selector: '[data-im="card-toolbar-host"]', kind: "main" },
  { selector: ".card", kind: "card" },
];

function matchesKnown(element: Element): { selector: string; kind: string } | null {
  for (const entry of KNOWN_CONTAINERS) {
    try {
      if (element.matches(entry.selector)) return entry;
    } catch {
      /* 选择器在某些环境不支持时跳过 */
    }
  }
  return null;
}

function isPositioned(element: Element): boolean {
  const inline = (element as HTMLElement).style?.position;
  if (inline && inline !== "static") return true;
  try {
    const computed = getComputedStyle(element).position;
    return Boolean(computed) && computed !== "static";
  } catch {
    return false;
  }
}

/**
 * 安装布局桩。root 通常是挂载后的 .board-shell。
 * 返回值必须在使用完后调用 restore()（afterEach）。
 */
export function installCardToolbarLayout(root: HTMLElement, options: LayoutOptions): CardToolbarHarness {
  const originalGetRect = Element.prototype.getBoundingClientRect;
  const warnings: string[] = [];
  let viewportRect = { ...options.viewport };
  let toolbarSize = options.toolbarSize ?? { w: 337.59, h: 35.25 };
  const cardNatural: Record<string, { w: number; h: number }> = { ...(options.cardNatural ?? {}) };
  const foldedHeight = options.foldedHeight ?? 60;

  const query = <T extends Element>(selector: string): T | null => root.querySelector(selector) as T | null;
  const viewportQuery = query<HTMLElement>(".board-viewport");
  const surfaceQuery = query<HTMLElement>(".board-surface");
  const scrollContent = query<HTMLElement>(".board-scroll-content");
  const host = query<HTMLElement>('[data-im="card-toolbar-host"]');
  if (!viewportQuery || !surfaceQuery) throw new Error("布局桩：找不到 .board-viewport / .board-surface");
  /** 显式收窄成非空：下面的闭包（scale/rectOf/measure）都要用，TS 不会跨闭包保留收窄结果。 */
  const viewport: HTMLElement = viewportQuery;
  const surface: HTMLElement = surfaceQuery;

  Object.defineProperty(viewport, "clientWidth", { value: viewportRect.width, configurable: true });
  Object.defineProperty(viewport, "clientHeight", { value: viewportRect.height, configurable: true });
  if (scrollContent) {
    Object.defineProperty(scrollContent, "clientWidth", { value: viewportRect.width, configurable: true });
    Object.defineProperty(scrollContent, "clientHeight", { value: viewportRect.height, configurable: true });
  }
  if (host) {
    Object.defineProperty(host, "offsetWidth", { value: viewportRect.width, configurable: true });
    Object.defineProperty(host, "offsetHeight", { value: viewportRect.height, configurable: true });
    Object.defineProperty(host, "clientWidth", { value: viewportRect.width, configurable: true });
    Object.defineProperty(host, "clientHeight", { value: viewportRect.height, configurable: true });
  }
  Object.defineProperty(viewport, "scrollLeft", {
    value: options.scroll?.x ?? 0,
    writable: true,
    configurable: true,
  });
  Object.defineProperty(viewport, "scrollTop", {
    value: options.scroll?.y ?? 0,
    writable: true,
    configurable: true,
  });

  function refreshScrollBounds(): void {
    const scaleX = scale().x;
    Object.defineProperty(viewport, "scrollWidth", {
      value: options.surfaceSize.w * scaleX,
      configurable: true,
    });
    Object.defineProperty(viewport, "scrollHeight", {
      value: options.surfaceSize.h * scale().y,
      configurable: true,
    });
  }

  function scale(): { x: number; y: number } {
    if (!surface) return { x: 1, y: 1 };
    return parseScale(inlineTransform(surface));
  }

  function liveScroll(): { x: number; y: number } {
    return {
      x: Number.isFinite(viewport.scrollLeft) ? viewport.scrollLeft : 0,
      y: Number.isFinite(viewport.scrollTop) ? viewport.scrollTop : 0,
    };
  }

  function viewportLayoutRect(): LayoutRect {
    return {
      left: viewportRect.left,
      top: viewportRect.top,
      right: viewportRect.left + viewportRect.width,
      bottom: viewportRect.top + viewportRect.height,
      width: viewportRect.width,
      height: viewportRect.height,
    };
  }

  function scrollContentRect(): LayoutRect {
    const scroll = liveScroll();
    const origin = { left: viewportRect.left - scroll.x, top: viewportRect.top - scroll.y };
    const scaleNow = scale();
    return {
      left: origin.left,
      top: origin.top,
      right: origin.left + options.surfaceSize.w * scaleNow.x,
      bottom: origin.top + options.surfaceSize.h * scaleNow.y,
      width: options.surfaceSize.w * scaleNow.x,
      height: options.surfaceSize.h * scaleNow.y,
    };
  }

  function surfaceRect(): LayoutRect {
    const base = scrollContentRect();
    const scaleNow = scale();
    return {
      left: base.left,
      top: base.top,
      right: base.left + options.surfaceSize.w * scaleNow.x,
      bottom: base.top + options.surfaceSize.h * scaleNow.y,
      width: options.surfaceSize.w * scaleNow.x,
      height: options.surfaceSize.h * scaleNow.y,
    };
  }

  function naturalSize(element: HTMLElement): { w: number; h: number } {
    const style = element.style;
    const id = element.getAttribute("data-card-id") ?? "";
    const fallback = cardNatural[id];
    const width = px(style?.width, fallback?.w ?? 240);
    const heightStyle = style?.height ?? "";
    const height = heightStyle && heightStyle !== "auto" ? px(heightStyle, fallback?.h ?? 96) : fallback?.h ?? foldedHeight;
    return { w: width, h: height };
  }

  function cardRectOf(element: HTMLElement): LayoutRect {
    const base = surfaceRect();
    const scaleNow = scale();
    const size = naturalSize(element);
    const left = base.left + px(element.style?.left, 0) * scaleNow.x;
    const top = base.top + px(element.style?.top, 0) * scaleNow.y;
    const width = size.w * scaleNow.x;
    const height = size.h * scaleNow.y;
    return { left, top, right: left + width, bottom: top + height, width, height };
  }

  function rectOf(element: Element): LayoutRect {
    if (!element) return ZERO;
    if (element === viewport || element === host || element.classList.contains("board-main") || element.classList.contains("board-shell")) {
      return viewportLayoutRect();
    }
    if (element.classList.contains("board-scroll-content")) return scrollContentRect();
    if (element.classList.contains("board-surface")) return surfaceRect();
    if (element.matches('[data-im="card"]')) return cardRectOf(element as HTMLElement);
    if (element.matches('[data-im="card-toolbar"]')) return toolbarRect() ?? ZERO;
    const natural = naturalSize(element as HTMLElement);
    return { left: 0, top: 0, right: natural.w, bottom: natural.h, width: natural.w, height: natural.h };
  }

  function containingBlockOf(element: Element): ContainingBlock | null {
    let node: Element | null = element.parentElement;
    while (node && node !== document.documentElement) {
      const known = matchesKnown(node);
      if (known) {
        const chain = productScale(node);
        return {
          selector: known.selector,
          element: node as HTMLElement,
          scale: chain,
          origin: { left: rectOf(node).left, top: rectOf(node).top },
        };
      }
      if (isPositioned(node)) {
        warnings.push(
          "未知定位祖先 " + describe(node) + "（父级链上出现桩没建模的 position 容器，几何可能算错）",
        );
        const chain = productScale(node);
        return { selector: describe(node), element: node as HTMLElement, scale: chain, origin: { left: 0, top: 0 } };
      }
      node = node.parentElement;
    }
    return null;
  }

  /** 从 element 往上（含自己）所有行内 transform 的 scale 乘积。 */
  function productScale(element: Element): { x: number; y: number } {
    let x = 1;
    let y = 1;
    let node: Element | null = element;
    while (node && node.nodeType === 1) {
      const parsed = parseScale(inlineTransform(node));
      x *= parsed.x;
      y *= parsed.y;
      node = node.parentElement;
    }
    return { x, y };
  }

  function toolbarElement(): HTMLElement | null {
    return query<HTMLElement>('[data-im="card-toolbar"]');
  }

  function toolbarRect(): LayoutRect | null {
    const toolbar = toolbarElement();
    if (!toolbar) return null;
    const block = containingBlockOf(toolbar);
    const chain = block ? block.scale : productScale(toolbar.parentElement ?? toolbar);
    const origin = block ? block.origin : { left: 0, top: 0 };
    const left = origin.left + px(toolbar.style?.left, 0) * chain.x;
    const top = origin.top + px(toolbar.style?.top, 0) * chain.y;
    const width = toolbarSize.w * chain.x;
    const height = toolbarSize.h * chain.y;
    return { left, top, right: left + width, bottom: top + height, width, height };
  }

  function measure(): ToolbarMeasurement {
    const toolbar = toolbarElement();
    const cardId = toolbar?.getAttribute("data-card-id") ?? null;
    const card = cardId ? query<HTMLElement>('[data-im="card"][data-card-id="' + cardId + '"]') : null;
    const cardRect = card ? cardRectOf(card) : null;
    const tr = toolbarRect();
    const style = toolbar ? getComputedStyle(toolbar) : null;
    const toolbarVisible = Boolean(
      toolbar && style && style.display !== "none" && style.visibility !== "hidden" && Number.parseFloat(style.opacity || "1") > 0.01,
    );
    const viewBox = viewportLayoutRect();
    const cardIntersectsViewport = cardRect
      ? cardRect.left < viewBox.right && viewBox.left < cardRect.right && cardRect.top < viewBox.bottom && viewBox.top < cardRect.bottom
      : null;
    return {
      toolbar,
      card,
      cardId,
      cardRect,
      toolbarRect: tr,
      toolbarVisible,
      toolbarInsideSurface: toolbar ? Boolean(toolbar.closest(".board-surface")) : false,
      toolbarParent: toolbar?.parentElement ? describe(toolbar.parentElement) : null,
      hostOwnerId: host ? host.getAttribute("data-owner-card-id") : null,
      hostVisible: host ? host.getAttribute("data-toolbar-visible") : null,
      dx: cardRect && tr ? tr.left - cardRect.left : null,
      gap: cardRect && tr ? cardRect.top - tr.bottom : null,
      cardIntersectsViewport,
      scale: scale().x,
      scroll: liveScroll(),
    };
  }

  function describe(element: Element): string {
    const tag = element.tagName.toLowerCase();
    const cls = (element.className && element.className.toString ? element.className.toString() : "")
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .join(".");
    const dataIm = element.getAttribute("data-im");
    return tag + (cls ? "." + cls : "") + (dataIm ? "[data-im=" + dataIm + "]" : "");
  }

  // 全局替换原型方法：所有组件测量都走这套模型
  Element.prototype.getBoundingClientRect = function getBoundingClientRectPatched(this: Element): DOMRect {
    const rect = rectOf(this);
    return {
      x: rect.left,
      y: rect.top,
      left: rect.left,
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
      width: rect.width,
      height: rect.height,
      toJSON: () => ({ ...rect }),
    } as DOMRect;
  };

  refreshScrollBounds();

  return {
    viewport,
    surface,
    scrollContent,
    host,
    scale: () => scale().x,
    scroll: liveScroll,
    setScroll(x: number, y: number) {
      viewport.scrollLeft = x;
      viewport.scrollTop = y;
    },
    setViewport(rect) {
      viewportRect = { ...rect };
      Object.defineProperty(viewport, "clientWidth", { value: rect.width, configurable: true });
      Object.defineProperty(viewport, "clientHeight", { value: rect.height, configurable: true });
      if (host) {
        Object.defineProperty(host, "offsetWidth", { value: rect.width, configurable: true });
        Object.defineProperty(host, "offsetHeight", { value: rect.height, configurable: true });
      }
    },
    setToolbarSize(size) {
      toolbarSize = { ...size };
    },
    setCardNatural(cardId, size) {
      cardNatural[cardId] = { ...size };
    },
    rectOf,
    cardElement(cardId) {
      return query<HTMLElement>('[data-im="card"][data-card-id="' + cardId + '"]');
    },
    cardRect(cardId) {
      const element = query<HTMLElement>('[data-im="card"][data-card-id="' + cardId + '"]');
      return element ? cardRectOf(element) : null;
    },
    toolbarElement,
    toolbarRect,
    containingBlockOf,
    measure,
    warnings,
    restore() {
      Element.prototype.getBoundingClientRect = originalGetRect;
    },
  };
}
