/**
 * 卡片局部工具栏的落点决策 —— 纯函数，便于在 jsdom 里逐条断言。
 *
 * 规则（2026-10-14 第二轮修，用户报告「工具栏与卡片有偏移、松手后还会漂」）：
 * 1. 工具栏左边缘 = 所属卡片**实际显示外框**的左边缘（0 偏差）；
 * 2. 工具栏底边 = 卡片顶边上方固定的 gap 屏幕像素；
 * 3. 不做横向夹取、不做上下翻转、不做侧移 —— 任何视口边缘都不改变这个相对位置。
 *    显隐由调用方决定：**卡片与板面可视区相交、且工具栏屏幕外框也与它相交**时才显示
 *    （严格相交；边贴边不算），见 {@link rectsIntersect}。隐藏只作用于显隐，不改位置。
 *
 * 为什么删掉了旧的避让语义（placeCardOverlay）：它把「不压住卡片」放在「固定相对位置」之上 ——
 * 上方放不下就翻到下方、右边放不下就侧移，于是拖动 / 松手 / 缩放时工具栏会跳到别处；
 * 而用户在同一时刻盯着卡片与工具栏，位置一跳就是「偏移」。现在只认卡片外框这一个锚点。
 *
 * 输入全部是**屏幕坐标**（getBoundingClientRect 原样），输出是**宿主定位原点**的像素：
 * 宿主（.card-toolbar-host）与所属卡片的相对关系是它唯一的坐标来源。
 * 宿主若真的处在缩放祖先下（实测 hostRect.width / host.offsetWidth ≠ 1），按同一实测比例逆变换 ——
 * 不用任何经验补偿常数。
 */
export interface OverlayRect {
  top: number;
  bottom: number;
  left: number;
  right: number;
}

/** 相对宿主定位原点的像素位置（直接写进行内 left / top）。 */
export interface ToolbarPlacement {
  left: number;
  top: number;
}

/** 宿主外框与自身布局盒的实测比例（1 = 无缩放祖先）。 */
export interface ToolbarScale {
  x: number;
  y: number;
}

export interface ToolbarAnchor {
  /** 所属卡片的真实显示外框（屏幕坐标，含边框） */
  cardRect: OverlayRect;
  /** 工具栏宿主的外框（屏幕坐标） */
  hostRect: OverlayRect;
  /** 工具栏实测高度（屏幕像素）；量不到时调用方用 TOOLBAR_H 兜底 */
  toolbarHeight: number;
  /** 工具栏实测宽度（屏幕像素）；提供时用于判断竖卡改贴左边 */
  toolbarWidth?: number;
  /** 工具栏底边与卡片顶边之间的固定间距（屏幕像素） */
  gap: number;
  /** 宿主处于缩放祖先下时的实测比例；缺省 / 非法都按 1（不缩放）处理 */
  hostScale?: ToolbarScale;
}

function safeScale(value: unknown): number {
  const scale = typeof value === "number" ? value : Number(value);
  return Number.isFinite(scale) && scale > 0 ? scale : 1;
}

/**
 * 固定锚点：左对齐卡片左边缘；底边在卡片顶边上方 gap。
 * 只做一次「屏幕坐标 → 宿主定位原点」的换算，外加宿主缩放比例的逆变换。
 */
export function placeToolbarRelativeToCard(input: ToolbarAnchor): ToolbarPlacement {
  const scaleX = safeScale(input.hostScale?.x);
  const scaleY = safeScale(input.hostScale?.y);
  const width = input.cardRect.right - input.cardRect.left;
  const height = input.cardRect.bottom - input.cardRect.top;
  const toolbarWidth = Number(input.toolbarWidth);
  /*
   * 长边优先：宽卡贴上边，竖卡贴左边。
   * 工具栏仍是一行图标；贴左边时整体放在卡片左侧、顶部对齐，避开左边中点端口。
   * 不提供 toolbarWidth 的旧调用保持原来的「贴上边」行为。
   */
  const placeLeft = Number.isFinite(toolbarWidth) && toolbarWidth > 0 && height > width * 1.08;
  if (placeLeft) {
    return {
      left: (input.cardRect.left - toolbarWidth - input.gap - input.hostRect.left) / scaleX,
      top: (input.cardRect.top - input.hostRect.top + 8) / scaleY,
    };
  }
  return {
    left: (input.cardRect.left - input.hostRect.left) / scaleX,
    top: (input.cardRect.top - input.hostRect.top - input.toolbarHeight - input.gap) / scaleY,
  };
}

/**
 * 两个矩形是否有交集（严格相交：边贴边不算）。
 * 只用于**可见性**判断 —— 卡片与板面可视区完全不相交时才隐藏工具栏，
 * 部分相交时照常按固定相对位置显示（绝不因此移动工具栏）。
 */
export function rectsIntersect(a: OverlayRect, b: OverlayRect): boolean {
  return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}
