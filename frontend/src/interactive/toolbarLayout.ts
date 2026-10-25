/**
 * 底部功能栏的「按实际可用宽度分档」逻辑（纯函数，可单测）。
 *
 * 为什么需要它（2026-10-15 布局重构）：
 *   旧实现用视口媒体查询（@media max-width: 1080px / 900px / 620px）决定功能栏的行列关系。
 *   于是「窗口很宽、但聊天面板 / 侧栏 / 缩放后的容器把功能栏挤窄」时，断点根本不触发：
 *   功能栏仍按宽档排一行，提交区的长说明被压成极窄竖列，整条栏被撑高。
 *   这里改成读**功能栏自己的可用宽度**（由 BoardToolbar 用探测器实测），再决定分档。
 *
 * 三档的行列关系（由 BoardToolbar 的 grid-template-areas 定义，避免随内容长度随机排列）：
 *   wide   ：第一行 [编辑操作 | 上次提交 + 查看过程]，第二行 [本次统计 | 详情 + 提交]，notes 整行
 *   mid    ：第一行 [编辑操作]，第二行 [本次统计 | 详情 + 提交]，第三行 [上次提交 + 查看过程]，notes 整行
 *   narrow ：编辑操作 / 本次统计 / 详情 + 提交（右对齐）/ 上次提交 + 查看过程 / notes，逐行铺开
 *
 * 阈值的来历：用真实渲染实测两档参考宽度（功能栏约 764px 与约 1178px）——
 * 764px 下两行主栏放不下「编辑操作 + 上次提交状态与查看过程」这一行（实测会被压出竖列），
 * 1178px 下两行主栏完整成立。所以 wide 的下界取在两者之间，且留出余量给更长的真实内容
 * （例如「已提交过：有新的改动尚未提交」这类更长的状态文字）。
 * 数值集中在这里，改了必须重跑真实渲染验收（scripts 见 _bt-evidence）。
 */
export type ToolbarBand = "wide" | "mid" | "narrow";

/** 进入 wide 档所需的最小可用宽度（px）。 */
export const TOOLBAR_WIDE_MIN = 900;
/** 进入 mid 档所需的最小可用宽度（px）；再窄就是 narrow。 */
export const TOOLBAR_MID_MIN = 560;
/**
 * 回宽档的滞回余量（px）：缩小时立即降档、放大时要多出这么多才升档。
 * 没有它，在阈值附近连续拖动窗口会来回跳档（要求「窗口连续改变尺寸时不出现反复跳动」）。
 */
export const TOOLBAR_BAND_HYSTERESIS = 40;

export interface ToolbarBandInput {
  /** 功能栏实际可用宽度（px）。量不到时给 0，表示「保持上一档」。 */
  available: number;
  /** 上一档；首次计算传 undefined。 */
  previous?: ToolbarBand;
}

/**
 * 由可用宽度决定分档。缩小时立即降档（宁可早一点排成多行，也不要把文字压成竖列）；
 * 放大时带滞回，避免在阈值附近抖动。
 */
export function toolbarBand({ available, previous }: ToolbarBandInput): ToolbarBand {
  const width = Number.isFinite(available) ? Math.max(0, available) : 0;
  // 量不到就保持上一档（jsdom / 首帧）：绝不用 0 当作「最窄」，否则测试与首帧都会被判成 narrow
  if (width <= 0) return previous ?? "wide";
  // 从宽档离开是「降档」：立即生效，宁可早排成多行，也不要把文字压成竖列
  if (previous === "wide") {
    if (width >= TOOLBAR_WIDE_MIN) return "wide";
    return width >= TOOLBAR_MID_MIN ? "mid" : "narrow";
  }
  // 回到宽档是「升档」：要多出滞回余量才回得去，避免阈值附近反复跳档
  if (width >= TOOLBAR_WIDE_MIN + TOOLBAR_BAND_HYSTERESIS) return "wide";
  if (previous === "mid") return width >= TOOLBAR_MID_MIN ? "mid" : "narrow";
  // previous === "narrow"
  return width >= TOOLBAR_MID_MIN + TOOLBAR_BAND_HYSTERESIS ? "mid" : "narrow";
}

function lengthOf(style: CSSStyleDeclaration, name: string, fallback: number): number {
  const value = Number.parseFloat(style.getPropertyValue(name));
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

/**
 * 量「功能栏实际可用宽度」。
 *
 * 它等于功能栏 max-width 的实际取值（变量定义在 styles/layers.css）：
 *   min(--im-toolbar-max-width, 100% - --im-toolbar-chat-reserve - --sp-8 - --sp-4)
 * 其中 100% 是舞台（[data-im="interactive-stage"]）的内容宽度。--im-toolbar-chat-reserve 是 min()/vw 组合，
 * `getPropertyValue` 拿到的是原始 token 串而不是像素，所以用一个宽度就等于该令牌的
 * 隐形探测器（reserveProbe）来量 —— 它不含相对自身容器的百分比，不会自引用。
 *
 * 量不到（jsdom / 还没布局）时返回 0，调用方据此保持上一档。
 */
export function toolbarAvailableWidth(el: HTMLElement, reserveProbe?: HTMLElement | null): number {
  if (typeof getComputedStyle !== "function") return 0;
  const owner = el.offsetParent as HTMLElement | null;
  const stage = owner ?? (el.parentElement as HTMLElement | null);
  const stageWidth = stage?.clientWidth ?? 0;
  if (!(stageWidth > 0)) return 0;

  const style = getComputedStyle(el);
  const probed = reserveProbe?.getBoundingClientRect?.().width ?? 0;
  const reserve = probed > 0 ? probed : lengthOf(style, "--im-toolbar-chat-reserve", 132);
  const edgeGap = lengthOf(style, "--sp-8", 32) + lengthOf(style, "--sp-4", 16);
  const rawMax = style.getPropertyValue("--im-toolbar-max-width").trim();
  const cap = /px$/i.test(rawMax) ? Number.parseFloat(rawMax) : stageWidth;
  const width = Math.min(Number.isFinite(cap) && cap > 0 ? cap : stageWidth, stageWidth - reserve - edgeGap);
  return Number.isFinite(width) && width > 0 ? width : 0;
}
