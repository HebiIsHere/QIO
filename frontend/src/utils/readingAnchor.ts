/**
 * 阅读位置的稳定锚点（纯函数，便于单测）。
 *
 * 旧实现只保存 scrollTop：虚拟列表先按每轮 120px 估算、再逐条实测真实高度，
 * 恢复时总高度还没稳定，旧像素会被截断到错误的「底部」，无法确认原阅读内容回到原位。
 *
 * 这里改为记录**稳定身份 + 视口相对偏移**：
 * - 轮次身份：turn_id（新消息）或该轮首条消息 id（旧历史）；
 * - 轮内锚点：视口里第一个仍可见的直接子块（消息 id / 过程区 / 其它卡片）；
 * - 文本锚点：视口顶部附近可见的文字片段（长回答内部、Markdown 段落）；
 * - 像素值只作为回退，不作为唯一依据。
 */
export interface ReadingAnchorCapture {
  turnKey: string;
  anchorKey: string | null;
  anchorOffset: number;
  turnOffset: number;
  textSnippet: string | null;
  textOffset: number | null;
}

/** 轮次稳定身份：优先 turn_id，旧历史退到该轮首条消息 id。 */
export function turnKeyOf(turn: {
  turnId?: string | null;
  items?: { id?: string | null }[];
}): string {
  const turnId = String(turn.turnId ?? "").trim();
  if (turnId) return turnId;
  const first = turn.items?.[0]?.id;
  return first ? String(first) : "";
}

/** 压成一行、去首尾空白：文本锚点必须能跨行匹配。 */
export function normalizeSnippet(value: unknown, limit = 28): string {
  return String(value ?? "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, limit);
}

/** 轮内锚定元素身份：消息 id 优先，其次 data-test，最后第一个 class。 */
export function anchorKeyOf(el: Element | null | undefined): string | null {
  if (!el) return null;
  const msgId = el.getAttribute("data-message-id");
  if (msgId) return "msg:" + msgId;
  const test = el.getAttribute("data-test");
  if (test) return "test:" + test;
  const cls = typeof el.className === "string" ? el.className.trim() : "";
  const first = cls.split(/\s+/).filter(Boolean)[0];
  return first ? "class:" + first : null;
}

/** 在轮次块里按身份找回锚定元素。 */
export function findAnchorElement(
  turnEl: HTMLElement | null,
  anchorKey: string | null,
): HTMLElement | null {
  if (!turnEl || !anchorKey) return null;
  for (const child of Array.from(turnEl.children)) {
    if (anchorKeyOf(child) === anchorKey) return child as HTMLElement;
  }
  return null;
}

/**
 * 视口顶部附近可见的文字片段。
 *
 * 优先用 Chromium 的 caretRangeFromPoint（能拿到「这一行具体是哪个文本节点」），
 * 环境不支持时退回 elementFromPoint 的第一个文本节点。取不到就返回 null ——
 * 没有文本锚点时用元素锚点 + 轮次偏移，不猜。
 */
export function textAnchorAtPoint(
  container: HTMLElement,
  clientX: number,
  clientY: number,
): { snippet: string; offset: number } | null {
  const doc = container.ownerDocument;
  if (!doc) return null;
  let textNode: Text | null = null;
  let start = 0;
  try {
    const caret = (doc as Document & {
      caretRangeFromPoint?: (x: number, y: number) => Range | null;
    }).caretRangeFromPoint?.(clientX, clientY);
    if (caret && caret.startContainer && caret.startContainer.nodeType === 3) {
      textNode = caret.startContainer as Text;
      start = caret.startOffset;
    }
  } catch {
    textNode = null;
  }
  if (!textNode) {
    try {
      const el = doc.elementFromPoint?.(clientX, clientY) as Element | null;
      if (el && container.contains(el)) {
        const walker = doc.createTreeWalker(el, NodeFilter.SHOW_TEXT);
        const first = walker.nextNode();
        if (first) {
          textNode = first as Text;
          start = 0;
        }
      }
    } catch {
      textNode = null;
    }
  }
  if (!textNode) return null;
  const raw = textNode.data || "";
  const snippet = normalizeSnippet(raw.slice(Math.max(0, start)), 28);
  // 太短的片段（例如单独一个标点 / 数字）不具备稳定身份，宁可不记
  if (snippet.length < 6) return null;
  const parent = textNode.parentElement;
  if (!parent || !container.contains(parent)) return null;
  try {
    const containerTop = container.getBoundingClientRect().top;
    return { snippet, offset: parent.getBoundingClientRect().top - containerTop };
  } catch {
    return null;
  }
}

/** 在容器里按文本片段找回元素（长回答内部位置）。 */
export function findSnippetElement(
  container: HTMLElement,
  snippet: string | null,
): HTMLElement | null {
  const target = normalizeSnippet(snippet, 60);
  if (target.length < 6) return null;
  const doc = container.ownerDocument;
  if (!doc) return null;
  try {
    const walker = doc.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    let node: Node | null = walker.nextNode();
    while (node) {
      const data = (node as Text).data || "";
      if (data.replace(/\s+/g, " ").includes(target)) {
        const parent = (node as Text).parentElement;
        if (parent && container.contains(parent)) return parent;
      }
      node = walker.nextNode();
    }
  } catch {
    return null;
  }
  return null;
}

/**
 * 捕获当前视口里的阅读锚点。
 *
 * 只读 DOM，不改滚动位置；拿不到渲染中的轮次（jsdom / 列表尚未布局）时返回 null，
 * 由调用方按旧像素回退 —— 不会把「量不到」伪装成「已经恢复」。
 */
export function captureReadingAnchor(container: HTMLElement): ReadingAnchorCapture | null {
  if (typeof container.getBoundingClientRect !== "function") return null;
  let containerRect: DOMRect;
  try {
    containerRect = container.getBoundingClientRect();
  } catch {
    return null;
  }
  const viewportTop = containerRect.top;
  const turnEls = Array.from(
    container.querySelectorAll<HTMLElement>("[data-turn-key]"),
  );
  let turnEl: HTMLElement | null = null;
  for (const candidate of turnEls) {
    const rect = candidate.getBoundingClientRect();
    if (rect.height > 0 && rect.bottom > viewportTop + 2) {
      turnEl = candidate;
      break;
    }
  }
  if (!turnEl) return null;
  const turnKey = turnEl.getAttribute("data-turn-key") || "";
  if (!turnKey) return null;
  const turnRect = turnEl.getBoundingClientRect();
  // 轮内第一个仍可见的直接子块（.turn-meta 是轮次标签，不承载阅读内容）
  let anchorEl: HTMLElement | null = null;
  for (const child of Array.from(turnEl.children)) {
    if (child.classList.contains("turn-meta")) continue;
    const rect = (child as HTMLElement).getBoundingClientRect();
    if (rect.height > 0 && rect.bottom > viewportTop + 2) {
      anchorEl = child as HTMLElement;
      break;
    }
  }
  const anchorRect = anchorEl ? anchorEl.getBoundingClientRect() : turnRect;
  // 文本锚点：容器水平中点、视口顶部下方 18px（越过轮次标签，落在正文上）
  const text = textAnchorAtPoint(
    container,
    containerRect.left + containerRect.width / 2,
    viewportTop + 18,
  );
  return {
    turnKey,
    anchorKey: anchorKeyOf(anchorEl),
    anchorOffset: anchorRect.top - viewportTop,
    turnOffset: turnRect.top - viewportTop,
    textSnippet: text?.snippet ?? null,
    textOffset: text?.offset ?? null,
  };
}

/** 当前锚定元素相对视口顶部的偏移（px）；元素不在文档里返回 null。 */
export function anchorOffsetOf(
  container: HTMLElement,
  element: HTMLElement | null,
): number | null {
  if (!element || !element.isConnected) return null;
  try {
    return element.getBoundingClientRect().top - container.getBoundingClientRect().top;
  } catch {
    return null;
  }
}
