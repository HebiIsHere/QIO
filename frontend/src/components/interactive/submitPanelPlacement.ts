/**
 * 提交详情面板的空间放置（PM-QIO032 #05 E2E 缺陷修复）。
 *
 * 缺陷：800×600、板面 5 项改动时详情面板实测 305px，矩形覆盖提交按钮并挡住任务入口，
 * 导致「详情打开时切任务」点不到。根因是面板高度只按视口比例限高（46vh），
 * 且位置依赖 CSS `bottom: 100%` 的包含块 —— 内容一多就向上/向下长到不该占的区域。
 *
 * 这里把「面板能占多大空间」变成可单测的纯函数：输入实测矩形，输出锚定位置与最大高度。
 * 口径：
 * 1. 面板底边永远在工具栏顶边上方 `gap` 处 → 工具栏里的提交按钮不会被面板覆盖；
 * 2. 面板顶边永远不低于 `safeTop`（任务入口底边 / 舞台顶边 + 余量）→ 任务入口不被遮挡；
 * 3. 高度 = min(上限, 剩余可用空间)，内容再多也只内部滚动，不随改动列表无限长高；
 * 4. 左侧与宽度跟随工具栏（宽度 = 功能栏完整宽度），不横向挤压按钮。
 * 不读 DOM、不读 store；组件负责量矩形并把结果写成 inline style。
 */
export const SUBMIT_PANEL_GAP = 8;
/** 高度上限（与旧 CSS 的 420px 同档，避免宽窗下无意义地铺满板面）。 */
export const SUBMIT_PANEL_MAX_HEIGHT = 420;

export interface SubmitPanelAnchor {
  left: number;
  top: number;
  width: number;
}

/** 面板定位祖先（offsetParent）的实测矩形；缺省视为视口容器。 */
export interface SubmitPanelContainer {
  left: number;
  top: number;
  bottom: number;
}

export interface SubmitPanelPlacementInput {
  toolbar: SubmitPanelAnchor;
  container?: SubmitPanelContainer | null;
  viewportHeight: number;
  /** 视口坐标：面板顶边不得高于这条线（任务入口底边 + 余量）。 */
  safeTop?: number;
  gap?: number;
  maxHeightCap?: number;
}

export interface SubmitPanelPlacement {
  /** 相对定位祖先的偏移：直接写 inline style（left / bottom / width / max-height）。 */
  left: number;
  bottom: number;
  width: number;
  maxHeight: number;
  /** 视口坐标下的面板顶边（写满 max-height 时）：用于断言不遮挡任务入口。 */
  top: number;
  /** 视口坐标下的面板底边：恒等于工具栏顶边 - gap，用于断言不覆盖提交按钮。 */
  bottomEdge: number;
  anchorTop: number;
}

function finite(value: unknown, fallback: number): number {
  const num = typeof value === "number" ? value : Number(value);
  return Number.isFinite(num) ? num : fallback;
}

function positive(value: unknown, fallback: number): number {
  const num = finite(value, fallback);
  return num > 0 ? num : fallback;
}

export function placeSubmitPanel(input: SubmitPanelPlacementInput): SubmitPanelPlacement {
  const viewportHeight = Math.max(0, finite(input.viewportHeight, 0));
  const gap = positive(input.gap, SUBMIT_PANEL_GAP);
  const cap = positive(input.maxHeightCap, SUBMIT_PANEL_MAX_HEIGHT);
  const anchorTop = finite(input.toolbar?.top, viewportHeight);
  const anchorLeft = finite(input.toolbar?.left, 0);
  const anchorWidth = Math.max(0, finite(input.toolbar?.width, 0));
  const safeTop = Math.max(0, finite(input.safeTop, 0));
  // 可用高度 = 工具栏顶边以上、且不越过 safeTop 的空间；夹在 [0, cap]。
  const maxHeight = Math.round(Math.max(0, Math.min(cap, anchorTop - gap - safeTop)));
  const container = input.container ?? null;
  const containerLeft = container ? finite(container.left, 0) : 0;
  const containerBottom = container ? finite(container.bottom, viewportHeight) : viewportHeight;
  const bottomEdge = Math.round(anchorTop - gap);
  return {
    left: Math.round(anchorLeft - containerLeft),
    // 相对定位祖先的 bottom：祖先底边到面板底边（视口坐标）的距离。
    bottom: Math.round(containerBottom - bottomEdge),
    width: Math.round(anchorWidth),
    maxHeight,
    top: Math.round(bottomEdge - maxHeight),
    bottomEdge,
    anchorTop: Math.round(anchorTop),
  };
}

/** 给定内容高度时面板的真实高度（内容不足时用内容高度，超出时只内部滚动）。 */
export function submitPanelHeight(placement: SubmitPanelPlacement, contentHeight: number): number {
  const content = Math.max(0, finite(contentHeight, 0));
  return Math.round(Math.min(content, placement.maxHeight));
}

/** 面板是否覆盖工具栏（提交按钮所在区域）：底边高于工具栏顶边才算安全。 */
export function submitPanelCoversToolbar(placement: SubmitPanelPlacement, contentHeight: number): boolean {
  const height = submitPanelHeight(placement, contentHeight);
  const top = placement.bottomEdge - height;
  return top < placement.anchorTop && placement.bottomEdge > placement.anchorTop;
}
